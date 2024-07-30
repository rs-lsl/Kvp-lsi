import torch
from torch import nn
import torch.optim as optim
import torch.nn.functional as F
from timm.models.layers import DropPath, trunc_normal_
import math
import numpy as np
from modules.vit import VisionTransformer

def cross_att_matrix(tensors, dim=[2], tar_dim=None):
    with torch.no_grad():
        mean = torch.mean(tensors, dim=dim, keepdim=True)#.type(torch.float32)
        std = torch.std(tensors, dim=dim, keepdim=True)#.type(torch.float32)
        normalized_tensor = (tensors - mean) / (std + 1e-6)

        cov_matrix = torch.einsum('ijk,ikl->ijl', normalized_tensor / (normalized_tensor.size(2) - 1), normalized_tensor[:, tar_dim].permute(0,2,1))
        mean_mat = torch.mean(torch.abs(cov_matrix), dim=-1)

    return mean_mat

class Pred_Model(nn.Module):
    def __init__(self, in_shape, out_ch, hid_S=32, hid_T=256, N_S=4, N_T=4,
                 mlp_ratio=8., drop=0.1, drop_path=0.0, spatio_kernel_enc=3,
                 spatio_kernel_dec=3, act_inplace=True, args=None, **kwargs):
        super(Pred_Model, self).__init__()
        B, T, C, H, W = in_shape  # T is input_time_length

        self.args = args
        self.hid_S = hid_S
        self.out_ch = out_ch

        # Encoder, Decoder
        act_inplace = False
        self.enc = Encoder(C, hid_S, N_S, spatio_kernel_enc, act_inplace=act_inplace, tar_dim=args.target_dim)        #   C_in, C_hid, N_S, spatio_kernel, act_inplace=True
        self.dec = Decoder(hid_S, out_ch, N_S, spatio_kernel_dec,
                           act_inplace=act_inplace)

        # embedding
        N_S_const = 2
        self.const_embedding = Encoder(args.ch_num_const, int(hid_S*0.5), N_S_const, spatio_kernel_enc, act_inplace=act_inplace)  #  hid_S  ***

        self.time_embedding = nn.Sequential(
                nn.Linear(args.input_time_length*args.time_emb_num, 128),
                nn.LeakyReLU(),
                nn.Linear(128, 256),   #  hid_S  ***
                nn.LeakyReLU(),
                nn.Linear(256, int(hid_S*0.5))
            )

        # prediction model
        scale_fac = 2
        num_block = 12
        N_S_pred = 4
        norm_band_num = 4  # in self attention
        patch_size = 4     # in self attention
        step = 2

        self.hid = [nn.Sequential(
            Encoder(int(hid_S * (T+1)), scale_fac * hid_S, N_S_pred, spatio_kernel_enc, act_inplace=act_inplace),
            VisionTransformer([H, W], patch_size=[4,4], inp_chans=scale_fac*hid_S, out_chans=scale_fac*hid_S,
                                     embed_dim=768, depth=8, num_heads=12, mlp_ratio=4., qkv_bias=True, mlp_drop_rate=0.0,
                                        attn_drop_rate=0.0, path_drop_rate=0.0, norm_layer="layer_norm", comm_inp_name="fin",
                                     comm_hidden_name="fout"),
            Decoder(scale_fac * hid_S, hid_S, N_S_pred, spatio_kernel_dec, act_inplace=act_inplace))
            for i in range(len(self.args.time_inte))]  #  args.drop
        self.hid = nn.ModuleList(self.hid)

    def forward(self, x_raw, const_data, time_data, labels, diff_ori=None, aft_seq_length=1, hid_i=0, shrink=1, mode='train', device=None, **kwargs):
        # print(x_raw.shape)
        B, T, C, H, W = x_raw.shape
        x = x_raw.view(B*T, C, H, W)
        embed = self.enc(x)
        _, C_, H_, W_ = embed.shape

        if mode == 'train':
            embed_label = self.enc(labels.detach().view(-1, C, H, W)).view(B, aft_seq_length, self.hid_S, H, W)
            label_pred = self.dec(embed).reshape((B, T, self.out_ch, H, W))
            embed_label_dec = self.dec(embed_label.view(-1, self.hid_S, H, W)).view(B, aft_seq_length, -1, H, W)
            # embed_label, label_pred, embed_diff, embed_tmp, embed_label_dec = None, None, None, None, None
            embed_tmp = embed.view(B, self.args.input_time_length, self.hid_S, H, W)
        else:
            embed_label, label_pred, embed_tmp, embed_label_dec = None, None, None, None

        z = embed.view(B, -1, H_, W_)#.gather()
        const_emb = self.const_embedding(const_data[None, ...])
        const_emb = const_emb.repeat(B, 1, 1, 1)
        if mode == 'train':
            hid = self._predict(z, const_emb, time_data, aft_seq_length, hid_i=hid_i, shrink=shrink, mode=mode,
                                device=device)
        else:
            hid = self._predict_pangu2(z, const_emb, time_data, aft_seq_length, shrink=shrink, mode=mode,
                                      device=device)
        hid = hid.reshape(B * aft_seq_length, -1, H_, W_)
        Y = self.dec(hid)   # , self.res_conv(skip).reshape(B*self.args.aft_seq_length, -1, H_, W_)
        Y = Y.reshape(B, aft_seq_length, self.out_ch, H, W)

        return Y, hid.view(B, aft_seq_length, self.hid_S, H, W), embed_label, label_pred, \
               embed_tmp, embed_label_dec

    def _predict_pangu2(self, cur_seq, cur_const_data, cur_time_data, aft_seq_length, shrink=False, mode='val',
                        device=None,
                        **kwargs):
        # pred_len means the var length that the model would predict
        pred_y = cur_seq.clone()
        # re_time_inte = self.args.time_inte[::-1]
        max_inte = max(self.args.time_inte)
        time_inte_dict = dict(zip(self.args.time_inte, np.arange(len(self.args.time_inte))))

        for pred_i in range(1, aft_seq_length + 1):
            iter_num = []  # iter_num[2,1,1]--inte:[4,2,1]

            pred_pos = pred_i % max_inte
            if pred_i == 1 or pred_i == 2:
                temp_pred_y = pred_y[:, -pred_pos*self.hid_S:-(pred_pos-1)*self.hid_S] if \
                    pred_i == 2 else pred_y[:, -pred_pos*self.hid_S:]
                cur_seq_tmp = self.forward_recur(torch.cat([pred_y[:, -pred_pos*2*self.hid_S:-(pred_pos*2-1)*self.hid_S],
                                                            temp_pred_y], 1),
                                                 cur_const_data, cur_time_data[:, :,
                                                                 [self.args.input_time_length - pred_pos - 1,
                                                                  self.args.input_time_length - 1]]
                                                 , hid_i=time_inte_dict[pred_pos])
            elif pred_i == 3:
                cur_seq_tmp = self.forward_recur(pred_y[:, -2*self.hid_S:],
                                                 cur_const_data, cur_time_data[:, :,
                                                                 [self.args.input_time_length,
                                                                  self.args.input_time_length + 1]]
                                                 , hid_i=time_inte_dict[1])
            else:
                cur_seq_tmp = self.forward_recur(torch.cat([pred_y[:, -8*self.hid_S:-7*self.hid_S], pred_y[:, -4*self.hid_S:-3*self.hid_S]], 1),
                                                 cur_const_data, cur_time_data[:, :,
                                                                 [self.args.input_time_length + pred_i - 8 - 1,
                                                                  self.args.input_time_length + pred_i - 4 - 1]]
                                                 , hid_i=time_inte_dict[4])
            pred_y = torch.cat([pred_y, cur_seq_tmp], 1)  # **********************************

        return pred_y[:, self.args.in_len_val*self.hid_S:]

    def _predict(self, cur_seq, cur_const_data, cur_time_data, aft_seq_length, hid_i=0, batch_y=None, shrink=0, mode='val', device=None, **kwargs):
        """Forward the model"""
        if aft_seq_length == self.args.pre_seq_length:
            pred_y = self.forward_recur(cur_seq, cur_const_data,
                            cur_time_data[:, :, 0*self.args.pre_seq_length:self.args.input_time_length+0*self.args.pre_seq_length], hid_i=hid_i)
        elif aft_seq_length < self.args.pre_seq_length:
            pred_y = self.forward_recur(cur_seq, cur_const_data, cur_time_data)
            pred_y = pred_y[:, :aft_seq_length]
        elif aft_seq_length > self.args.pre_seq_length:
            pred_y = []
            d = aft_seq_length // self.args.pre_seq_length
            m = aft_seq_length % self.args.pre_seq_length

            for i in range(d):
                # print(i)
                if shrink:   # means the output length is shorter than the input
                    if mode == 'train':
                        cur_seq = cur_seq + (torch.randn(size=cur_seq.size(), dtype=torch.float32)/100.0).type(torch.float32).to(device)  # ablation
                    temp_out = self.forward_recur(cur_seq, cur_const_data,
                                                  cur_time_data[:, :, i*self.args.pre_seq_length:i*self.args.pre_seq_length+self.args.input_time_length], hid_i=hid_i)
                    cur_seq = torch.cat([cur_seq[:, -(cur_seq.shape[1]-temp_out.shape[1]):, ...], temp_out], 1)
                    pred_y.append(temp_out)
                else:
                    if mode == 'train':
                        cur_seq = cur_seq + (torch.randn(size=cur_seq.size(), dtype=torch.float32)/100.0).type(torch.float32).to(device)  # ablation
                    cur_seq = self.forward_recur(cur_seq, cur_const_data,
                                                 cur_time_data[:, :, i*self.args.input_time_length:(i+1)*self.args.input_time_length], hid_i=hid_i)      # assume that the length of input and output of the model is same
                    pred_y.append(cur_seq)

            if m != 0:
                cur_seq = self.forward_recur(cur_seq, cur_const_data, cur_time_data[:, :, -(m+self.args.input_time_length+self.args.pre_seq_length):-m])
                pred_y.append(cur_seq[:, :m])

            pred_y = torch.cat(pred_y, dim=1)
        return pred_y

    def forward_recur(self, x, const_emb, time_data, hid_i=0, **kwargs):   # const_emb coube be a const, but the time is changed
        B, C, H, W = x.shape
        x_res = x[:, -self.hid_S:, ...]#.clone()

        time_emb = self.time_embedding(time_data.reshape(B, -1))[..., None, None].repeat(1, 1, H, W)#reshape(B, -1, H_, W_)#.contigous().view(B, C_, H_, W_)
        Y = self.hid[hid_i](torch.cat([x, const_emb, time_emb], 1))

        return Y + x_res

class BasicConv2d(nn.Module):

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size=3,
                 stride=1,
                 padding=0,
                 dilation=1,
                 upsampling=False,
                 act_norm=False,
                 act_inplace=True):
        super(BasicConv2d, self).__init__()
        self.act_norm = act_norm
        self.in_channels = in_channels
        if upsampling is True:
            self.conv = nn.Sequential(*[
                nn.Conv2d(in_channels, out_channels*4, kernel_size=kernel_size,
                          stride=1, padding=padding, dilation=dilation, padding_mode='circular'),
                nn.PixelShuffle(2)
            ])
        else:
            self.conv = nn.Conv2d(
                in_channels, out_channels, kernel_size=kernel_size,
                stride=stride, padding=padding, dilation=dilation, padding_mode='circular')

        self.norm = nn.GroupNorm(2, out_channels)   # group number: 2
        # self.norm = nn.BatchNorm2d(out_channels)
        self.act = nn.SiLU(inplace=act_inplace)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, (nn.Conv2d)):
            trunc_normal_(m.weight, std=.02)  # math.sqrt(2.0 / self.in_channels)
            nn.init.constant_(m.bias, 0)

    def forward(self, x):
        y = self.conv(x)
        if self.act_norm:
            y = self.act(self.norm(y))
        return y


class ConvSC(nn.Module):

    def __init__(self,
                 C_in,
                 C_out,
                 kernel_size=3,
                 downsampling=False,
                 upsampling=False,
                 act_norm=True,
                 act_inplace=True):
        super(ConvSC, self).__init__()

        stride = 2 if downsampling is True else 1
        padding = (kernel_size - stride + 1) // 2

        self.conv = BasicConv2d(C_in, C_out, kernel_size=kernel_size, stride=stride,
                                upsampling=upsampling, padding=padding,
                                act_norm=act_norm, act_inplace=act_inplace)
        self.conv2 = BasicConv2d(C_in, C_out, kernel_size=5, stride=stride,
                                upsampling=upsampling, padding=2,
                                act_norm=act_norm, act_inplace=act_inplace)
        self.cat_conv = nn.Conv2d(C_out*2, C_out, 1)

        self.res_conv = nn.Conv2d(C_in, C_out, kernel_size=3, stride=1, padding=1,
                                  padding_mode='circular') if C_in != C_out else nn.Identity()

    def forward(self, x):
        # x0 = x.clone()
        y = self.conv(x)
        y2 = self.conv2(x)
        return self.cat_conv(torch.cat([y, y2], 1)) + self.res_conv(x)


def sampling_generator(N, reverse=False):
    samplings = [False, False] * (N // 2)
    if reverse: return list(reversed(samplings[:N]))
    else: return samplings[:N]

class Encoder(nn.Module):
    def __init__(self, C_in, C_hid, N_S, spatio_kernel, act_inplace=True, tar_dim=None):
        samplings = sampling_generator(N_S)
        # print(samplings)
        super(Encoder, self).__init__()
        self.tar_dim = tar_dim
        if tar_dim is not None:
            self.corr_linear = nn.Sequential(
                nn.Linear(C_in, C_in//2),
                nn.SiLU(),
                nn.Linear(C_in//2, C_in),
                nn.Sigmoid())
        self.enc = nn.Sequential(
              ConvSC(C_in, (C_in+C_hid)//2, spatio_kernel, downsampling=samplings[0],
                     act_inplace=act_inplace),
              ConvSC((C_in+C_hid)//2, C_hid, spatio_kernel, downsampling=samplings[1],
                   act_inplace=act_inplace),
            *[ConvSC(C_hid, C_hid, spatio_kernel, downsampling=s,
                     act_inplace=act_inplace) for s in samplings[2:]]
        )

    def forward(self, x):  # B*4, 3, 128, 128
        # print(x.shape)
        B, C, _, _ = x.shape
        if self.tar_dim is not None:
            corr_mat = cross_att_matrix(x.view(B, C, -1), tar_dim=self.tar_dim)
            x = x * self.corr_linear(corr_mat)[..., None, None]

        latent = self.enc[0](x)
        for i in range(1, len(self.enc)):
            latent = self.enc[i](latent)
        return latent    #, enc1


class Decoder(nn.Module):

    def __init__(self, C_hid, C_out, N_S, spatio_kernel, act_inplace=True):
        samplings = sampling_generator(N_S, reverse=True)
        super(Decoder, self).__init__()
        mid_ch = (C_hid+C_out)//2 if ((C_hid+C_out)//2) % 2 ==0 else (C_hid+C_out)//2 + 1
        self.dec = nn.Sequential(
            *[ConvSC(C_hid, C_hid, spatio_kernel, upsampling=s,
                     act_inplace=act_inplace) for s in samplings[:-1]],
              ConvSC(C_hid, mid_ch, spatio_kernel, upsampling=samplings[-1],
                     act_inplace=act_inplace)
        )
        self.readout = nn.Conv2d(mid_ch, C_out, 3, padding=1, padding_mode='circular')

    def forward(self, hid, enc1=None):
        for i in range(0, len(self.dec)-1):
            hid = self.dec[i](hid)
        Y = self.dec[-1](hid)  #  + enc1
        Y = self.readout(Y)
        return Y

