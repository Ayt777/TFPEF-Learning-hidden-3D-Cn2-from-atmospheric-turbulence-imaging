import torch
import math
import numpy as np
from timm.models.layers import DropPath, trunc_normal_, activations
from torch import nn
from torch.nn import init
import torch.nn.functional as F
from torch.nn.modules.utils import _triple
from torch.nn.parameter import Parameter
from openstl.modules import (ConvSC, ConvNeXtSubBlock, ConvMixerSubBlock, GASubBlock, gInception_ST,
                             HorNetSubBlock, MLPMixerSubBlock, MogaSubBlock, PoolFormerSubBlock,
                             SwinSubBlock, UniformerSubBlock, VANSubBlock, ViTSubBlock, TAUSubBlock)


class SimVP_Model(nn.Module):
    r"""SimVP Model

    Implementation of `SimVP: Simpler yet Better Video Prediction
    <https://arxiv.org/abs/2206.05099>`_.

    """

    def __init__(self, in_shape, batch_size, hid_S=16, hid_T=256, N_S=4, N_T=4, model_type='gSTA',
                 mlp_ratio=8., drop=0.0, drop_path=0.0, spatio_kernel_enc=3,
                 spatio_kernel_dec=3, act_inplace=True, **kwargs):
        super(SimVP_Model, self).__init__()
        T, C, H, W = in_shape  # T is pre_seq_length
        # H, W = int(H / 2**(N_S/2)), int(W / 2**(N_S/2))  # downsample 1 / 2**(N_S/2)
        act_inplace = False
        self.enc = Encoder(C, hid_S, N_S, spatio_kernel_enc, T, batch_size, act_inplace=act_inplace)
        self.dec = Decoder(hid_S, C, N_S, spatio_kernel_dec, T, batch_size, act_inplace=act_inplace)

        model_type = 'gsta' if model_type is None else model_type.lower()
        if model_type == 'incepu':
            self.hid = MidIncepNet(T*hid_S, hid_T, N_T)
        else:
            self.hid = MidMetaNet(T*hid_S, hid_T, T, N_T,
                input_resolution=(H, W), model_type=model_type,
                mlp_ratio=mlp_ratio, drop=drop, drop_path=drop_path)
            # self.hid = MidMetaNet(T*C//2, T*C*2, T, N_T,
            #     input_resolution=(H, W), model_type=model_type,
            #     mlp_ratio=mlp_ratio, drop=drop, drop_path=drop_path)

    def forward(self, x_raw, **kwargs):
        B, T, C, H, W = x_raw.shape
        # print(B, T, C, H, W)
        # copy = x_raw.clone()
        x_diff = (x_raw[:,1:] - x_raw[:,:-1]).reshape(B, (T-1),C, H, W)
        x = x_raw.view(B*T, C, H, W)
        embed, skip = self.enc(x,x_diff)
        _, C_, H_, W_ = embed.shape
        z = embed.view(B, T, C_, H_, W_)
        # skip = skip.view(B, T, C_, 2*H_, 2*W_)
        hid = self.hid(z)
        # hid_skip = self.hid(skip)
        # hid = hid.reshape(B*T, C_, H_, W_)
        hid = hid.reshape(-1, C_, H_, W_)
        # hid_skip = hid_skip.reshape(B*T, C_, 2*H_, 2*W_)

        Y = self.dec(hid, skip)
        Y = Y.reshape(B, T, C, H, W)
        return Y
    
def sampling_generator(N, reverse=False):
    samplings = [False] * (N-1) + [True]
    if reverse: return list(reversed(samplings[:N]))
    else: return samplings[:N]


class Encoder(nn.Module):
    """3D Encoder for SimVP"""

    def __init__(self, C_in, C_hid, N_S, spatio_kernel, seq_len, batch_size, act_inplace=True, use_diff=True):
        samplings = sampling_generator(N_S)
        print(samplings)
        # print(C_in)
        super(Encoder, self).__init__()
        self.use_diff = use_diff
        self.diff = nn.Sequential(
            nn.Conv3d(seq_len-1, (seq_len-1)*2, kernel_size=3, stride=1, padding=1, bias=False),
            # nn.BatchNorm3d((seq_len-1)*2, momentum=0.01),
            nn.Conv3d((seq_len-1)*2, 1, kernel_size=3, stride=1, padding=1, bias=False),
            # nn.BatchNorm3d(1, momentum=0.01),
            nn.GELU(),
            # nn.AdaptiveAvgPool3d((20,30,30))
        )
        self.enc = nn.Sequential(
              ConvSC(C_in, C_hid, seq_len, spatio_kernel, downsampling=samplings[0],
                     act_inplace=act_inplace,use_tada=False),
            *[ConvSC(C_hid, C_hid, seq_len, spatio_kernel, downsampling=s,
                     act_inplace=act_inplace,use_tada=False) for s in samplings[1:]]
        )
        

    def forward(self, x, x_diff): 
        # diff 
        B, _, C, H, W = x_diff.shape
        x = x.view(B,-1, C, H, W)
        # print(x.shape)
        if self.use_diff:
            x_diff = self.diff(x_diff)
            # x = self.pool(x)
            # 4->4
            x = 0.93 * x + 0.07 * x_diff
            # 4->8
            # x = 0.9 * x + 0.1 * x_diff
            # x = 0.8 * x + 0.2 * x_diff
            # 4->16
            # x = 0.9 * x + 0.1 * x_diff
            # x = 0.85 * x + 0.15 * x_diff
            # x = x + x_diff
        enc1 = self.enc[0](x)
        latent = enc1
        for i in range(1, len(self.enc)):
            latent = self.enc[i](latent)
        return latent, enc1

class LKA(nn.Module):
    def __init__(self, dim):
        super().__init__()
        # self.bn = nn.BatchNorm3d(dim, momentum=0.01)
        self.conv0 = nn.Conv3d(dim, dim, 5, padding=2, groups=dim)
        self.conv_spatial = nn.Conv3d(dim, dim, 11, 1, padding=15, groups=dim, dilation=3)
        # self.conv_spatial = nn.Conv3d(dim, dim, 7, 1, padding=9, groups=dim, dilation=3)
        self.conv1 = nn.Conv3d(dim, dim, 1)

        self.reduction = 4
        # self.avg_pool = nn.AdaptiveAvgPool3d(1)
        self.fc = nn.Sequential(
            nn.Linear(dim, dim * self.reduction, bias=False), # reduction
            nn.ReLU(True),
            nn.Linear(dim * self.reduction, dim, bias=False), # expansion
            nn.Sigmoid()
        )


    def forward(self, x):
        u = x.clone()
        # x = self.bn(x)
        attn_1 = self.conv0(x)
        attn_2 = self.conv_spatial(attn_1)
        attn_3 = self.conv1(attn_2)

        b, t, _, _, _ = x.size()
        # se_atten = self.avg_pool(x).view(b, t)
        # # se_atten = self.avg_pool(attn_3).view(b, t)+self.avg_pool(attn_2).view(b, t)
        # se_atten = self.fc(se_atten).view(b, t, 1, 1, 1)

        return attn_3 * u


class Attention(nn.Module):
    def __init__(self, d_model=1, d_model2=64):
        super().__init__()
        # self.bn0 = nn.BatchNorm3d(d_model2, momentum=0.01)
        self.proj_1 = nn.Conv3d(d_model, d_model2, 1)
        self.activation = nn.GELU()
        self.spatial_gating_unit = LKA(d_model2)
        self.proj_2 = nn.Conv3d(d_model2, d_model, 1)

        self.conv1 = nn.Conv3d(d_model, d_model2, kernel_size=3, stride=1, padding=1, bias=False)
        self.bn1 = nn.BatchNorm3d(d_model2, momentum=0.01)
        self.conv2 = nn.Conv3d(d_model2, d_model2, kernel_size=3, stride=1, padding=1, bias=False)
        self.bn2 = nn.BatchNorm3d(d_model2, momentum=0.01)
        self.relu = nn.GELU()
        self.conv3 = nn.Conv3d(d_model2, d_model, kernel_size=1, stride=1, padding=0, bias=False)
        self.bn3 = nn.BatchNorm3d(d_model, momentum=0.01)


    def forward(self, x):
        shorcut = x.clone()
        x = self.proj_1(x)
        x = self.activation(x)
        # x = self.bn0(x)
        x = self.spatial_gating_unit(x)
        x = self.proj_2(x)
        x = x + shorcut
        x = self.conv1(x)# + self.weight_mask_conv(weight_mask)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.conv2(x)
        x = self.bn2(x)
        x = self.relu(x)
        x = self.conv3(x)
        return x

class Decoder(nn.Module):
    """3D Decoder for SimVP"""

    def __init__(self, C_hid, C_out, N_S, spatio_kernel, seq_len, batch_size,act_inplace=True):
        samplings = sampling_generator(N_S, reverse=True)
        super(Decoder, self).__init__()
        self.dec = nn.Sequential(
            *[ConvSC(C_hid, C_hid, seq_len, spatio_kernel, upsampling=s,
                     act_inplace=act_inplace,use_tada=False) for s in samplings[:-1]],
              ConvSC(C_hid, C_hid, seq_len, spatio_kernel, upsampling=samplings[-1],
                     act_inplace=act_inplace,use_tada=False),
        )
        self.T = seq_len
        self.refine = Attention(seq_len,5*seq_len)
        self.readout = nn.Conv2d(C_hid, C_out, 1)

    def forward(self, hid, enc1=None):
        for i in range(0, len(self.dec)-1):
            hid = self.dec[i](hid)
        Y = self.dec[-1](hid + enc1)
        _,c,h,w = Y.shape
        Y = Y.reshape(-1,self.T,c,h,w)
        Y = self.refine(Y)
        Y = Y.reshape(-1,c,h,w)
        Y = self.readout(Y)
        return Y


class MidIncepNet(nn.Module):
    """The hidden Translator of IncepNet for SimVPv1"""

    def __init__(self, channel_in, channel_hid, N2, incep_ker=[3,5,7,11], groups=8, **kwargs):
        super(MidIncepNet, self).__init__()
        assert N2 >= 2 and len(incep_ker) > 1
        self.N2 = N2
        enc_layers = [gInception_ST(
            channel_in, channel_hid//2, channel_hid, incep_ker= incep_ker, groups=groups)]
        for i in range(1,N2-1):
            enc_layers.append(
                gInception_ST(channel_hid, channel_hid//2, channel_hid,
                              incep_ker=incep_ker, groups=groups))
        enc_layers.append(
                gInception_ST(channel_hid, channel_hid//2, channel_hid,
                              incep_ker=incep_ker, groups=groups))
        dec_layers = [
                gInception_ST(channel_hid, channel_hid//2, channel_hid,
                              incep_ker=incep_ker, groups=groups)]
        for i in range(1,N2-1):
            dec_layers.append(
                gInception_ST(2*channel_hid, channel_hid//2, channel_hid,
                              incep_ker=incep_ker, groups=groups))
        dec_layers.append(
                gInception_ST(2*channel_hid, channel_hid//2, channel_in,
                              incep_ker=incep_ker, groups=groups))

        self.enc = nn.Sequential(*enc_layers)
        self.dec = nn.Sequential(*dec_layers)

    def forward(self, x):
        B, T, C, H, W = x.shape
        x = x.reshape(B, T*C, H, W)

        # encoder
        skips = []
        z = x
        for i in range(self.N2):
            z = self.enc[i](z)
            if i < self.N2-1:
                skips.append(z)
        # decoder
        z = self.dec[0](z)
        for i in range(1,self.N2):
            z = self.dec[i](torch.cat([z, skips[-i]], dim=1) )

        y = z.reshape(B, T, C, H, W)
        return y


class MetaBlock(nn.Module):
    """The hidden Translator of MetaFormer for SimVP"""

    def __init__(self, in_channels, out_channels, seq_len, input_resolution=None, model_type=None,
                 mlp_ratio=8., drop=0.0, drop_path=0.0, layer_i=0):
        super(MetaBlock, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        model_type = model_type.lower() if model_type is not None else 'gsta'

        if model_type == 'gsta':
            self.block = GASubBlock(
                in_channels, seq_len, kernel_size=21, mlp_ratio=mlp_ratio,
                drop=drop, drop_path=drop_path, act_layer=nn.GELU)
        elif model_type == 'convmixer':
            self.block = ConvMixerSubBlock(in_channels, kernel_size=11, activation=nn.GELU)
        elif model_type == 'convnext':
            self.block = ConvNeXtSubBlock(
                in_channels, mlp_ratio=mlp_ratio, drop=drop, drop_path=drop_path)
        elif model_type == 'hornet':
            self.block = HorNetSubBlock(in_channels, mlp_ratio=mlp_ratio, drop_path=drop_path)
        elif model_type in ['mlp', 'mlpmixer']:
            self.block = MLPMixerSubBlock(
                in_channels, input_resolution, mlp_ratio=mlp_ratio, drop=drop, drop_path=drop_path)
        elif model_type in ['moga', 'moganet']:
            self.block = MogaSubBlock(
                in_channels, mlp_ratio=mlp_ratio, drop_rate=drop, drop_path_rate=drop_path)
        elif model_type == 'poolformer':
            self.block = PoolFormerSubBlock(
                in_channels, mlp_ratio=mlp_ratio, drop=drop, drop_path=drop_path)
        elif model_type == 'swin':
            self.block = SwinSubBlock(
                in_channels, input_resolution, layer_i=layer_i, mlp_ratio=mlp_ratio,
                drop=drop, drop_path=drop_path)
        elif model_type == 'uniformer':
            block_type = 'MHSA' if in_channels == out_channels and layer_i > 0 else 'Conv'
            self.block = UniformerSubBlock(
                in_channels, mlp_ratio=mlp_ratio, drop=drop,
                drop_path=drop_path, block_type=block_type)
        elif model_type == 'van':
            self.block = VANSubBlock(
                in_channels, mlp_ratio=mlp_ratio, drop=drop, drop_path=drop_path, act_layer=nn.GELU)
        elif model_type == 'vit':
            self.block = ViTSubBlock(
                in_channels, mlp_ratio=mlp_ratio, drop=drop, drop_path=drop_path)
        elif model_type == 'tau':
            self.block = TAUSubBlock(
                in_channels, kernel_size=21, mlp_ratio=mlp_ratio,
                drop=drop, drop_path=drop_path, act_layer=nn.GELU)
        else:
            assert False and "Invalid model_type in SimVP"

        if in_channels != out_channels:
            self.reduction = nn.Conv2d(
                in_channels, out_channels, kernel_size=1, stride=1, padding=0)

    def forward(self, x):
        z = self.block(x)
        return z if self.in_channels == self.out_channels else self.reduction(z)
        # return z


class MidMetaNet(nn.Module):
    """The hidden Translator of MetaFormer for SimVP"""

    def __init__(self, channel_in, channel_hid, seq_len, N2,
                 input_resolution=None, model_type=None,
                 mlp_ratio=4., drop=0.0, drop_path=0.1):
        super(MidMetaNet, self).__init__()
        assert N2 >= 2 and mlp_ratio > 1
        self.N2 = N2
        dpr = [  # stochastic depth decay rule
            x.item() for x in torch.linspace(1e-2, drop_path, self.N2)]

        # downsample
        enc_layers = [MetaBlock(
            channel_in, channel_hid, seq_len, input_resolution, model_type,
            mlp_ratio, drop, drop_path=dpr[0], layer_i=0)]
        # middle layers
        for i in range(1, N2-1):
            enc_layers.append(MetaBlock(
                channel_hid, channel_hid, seq_len, input_resolution, model_type,
                mlp_ratio, drop, drop_path=dpr[i], layer_i=i))
        # upsample
        enc_layers.append(MetaBlock(
            channel_hid, channel_in, seq_len, input_resolution, model_type,
            mlp_ratio, drop, drop_path=drop_path, layer_i=N2-1))
        self.enc = nn.Sequential(*enc_layers)


    def forward(self, x):
        B, T, C, H, W = x.shape
        u = x.clone()
        x = x.reshape(B, T*C, H, W)

        z = x
        for i in range(self.N2):
            z = 0.94*self.enc[i](z) + 0.06*z if i in range(1, self.N2-1) else 0.06*self.enc[i](z)
            # z = self.enc[i](z)
        # z = self.enc[0](z)
        z = z.reshape(B, T, C, H, W)
        return z



if __name__=="__main__":
    model = SimVP_Model(in_shape=[10, 20, 30, 30],hid_S=64,hid_T=256,N_S=3,N_T=8)
    input = torch.rand((1, 10, 20, 30, 30))
    out = model(input)
    print(out.shape)