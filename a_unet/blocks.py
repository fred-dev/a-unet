from math import pi
from typing import Any, Callable, Optional, Sequence, Type, TypeVar, Union, List

import torch
import torch.nn.functional as F
from einops import pack, rearrange, reduce, repeat, unpack
from torch import Tensor, einsum, nn
from typing_extensions import TypeGuard

V = TypeVar("V")

"""
Helper functions
"""


class T:
    """Where the magic happens, builds a type template for a given type"""

    def __init__(self, t: Callable, override: bool = True):
        self.t = t
        self.override = override

    def __call__(self, *a, **ka):
        t, override = self.t, self.override

        class Inner:
            def __init__(self):
                self.args = a
                self.__dict__.update(**ka)

            def __call__(self, *b, **kb):
                if override:
                    return t(*(*a, *b), **{**ka, **kb})
                else:
                    return t(*(*b, *a), **{**kb, **ka})

        return Inner()


def Ts(t: Callable[..., V]) -> Callable[..., Callable[..., V]]:
    """Builds a type template for a given type that accepts a list of instances"""
    return lambda *types: lambda: t(*[tp() for tp in types])


def exists(val: Optional[V]) -> TypeGuard[V]:
    return val is not None


def default(val: Optional[V], d: V) -> V:
    return val if exists(val) else d


def Module(modules: Sequence[nn.Module], forward_fn: Callable):
    """Functional module helper"""

    class Module(nn.Module):
        def __init__(self):
            super().__init__()
            self.blocks = nn.ModuleList(modules)

        def forward(self, *args, **kwargs):
            return forward_fn(*args, **kwargs)

    return Module()


class Sequential(nn.Module):
    """Custom Sequential that includes all args"""

    def __init__(self, *blocks):
        super().__init__()
        self.blocks = nn.ModuleList(blocks)

    def forward(self, x: Tensor, *args) -> Tensor:
        for block in self.blocks:
            x = block(x, *args)
        return x


def Select(args_fn: Callable) -> Callable[..., Type[nn.Module]]:
    """Selects (swap, remove, repeat) forward arguments given a (lambda) function"""

    def fn(block_t: Type[nn.Module]) -> Type[nn.Module]:
        class Select(nn.Module):
            def __init__(self, *args, **kwargs):
                super().__init__()
                self.block = block_t(*args, **kwargs)
                self.args_fn = args_fn

            def forward(self, *args, **kwargs):
                return self.block(*args_fn(*args), **kwargs)

        return Select

    return fn


class Packed(Sequential):
    """Packs, and transposes non-channel dims, useful for attention-like view"""

    def forward(self, x: Tensor, *args) -> Tensor:
        x, ps = pack([x], "b d *")
        x = rearrange(x, "b d n -> b n d")
        x = super().forward(x, *args)
        x = rearrange(x, "b n d -> b d n")
        x = unpack(x, ps, "b d *")[0]
        return x


def Repeat(m: Union[nn.Module, Type[nn.Module]], times: int) -> Any:
    ms = (m,) * times
    return Sequential(*ms) if isinstance(m, nn.Module) else Ts(Sequential)(*ms)


def Skip(merge_fn: Callable[[Tensor, Tensor], Tensor] = torch.add) -> Type[Sequential]:
    class Skip(Sequential):

        """Adds skip connection around modules"""

        def forward(self, x: Tensor, *args) -> Tensor:
            return merge_fn(x, super().forward(x, *args))

    return Skip


"""
Modules
"""


def Conv(dim: int, *args, **kwargs) -> nn.Module:
    return [nn.Conv1d, nn.Conv2d, nn.Conv3d][dim - 1](*args, **kwargs)


def ConvTranspose(dim: int, *args, **kwargs) -> nn.Module:
    return [nn.ConvTranspose1d, nn.ConvTranspose2d, nn.ConvTranspose3d][dim - 1](
        *args, **kwargs
    )


def Downsample(
    dim: int, factor: int = 2, width: int = 1, conv_t=Conv, **kwargs
) -> nn.Module:
    width = width if factor > 1 else 1
    return conv_t(
        dim=dim,
        kernel_size=factor * width,
        stride=factor,
        padding=(factor * width - factor) // 2,
        **kwargs,
    )


def Upsample(
    dim: int,
    factor: int = 2,
    width: int = 1,
    conv_t=Conv,
    conv_tranpose_t=ConvTranspose,
    **kwargs,
) -> nn.Module:
    width = width if factor > 1 else 1
    return conv_tranpose_t(
        dim=dim,
        kernel_size=factor * width,
        stride=factor,
        padding=(factor * width - factor) // 2,
        **kwargs,
    )


def UpsampleInterpolate(
    dim: int,
    factor: int = 2,
    kernel_size: int = 3,
    mode: str = "nearest",
    conv_t=Conv,
    **kwargs,
) -> nn.Module:
    assert kernel_size % 2 == 1, "upsample kernel size must be odd"
    return nn.Sequential(
        nn.Upsample(scale_factor=factor, mode=mode),
        conv_t(
            dim=dim, kernel_size=kernel_size, padding=(kernel_size - 1) // 2, **kwargs
        ),
    )


def ConvBlock(
    dim: int,
    in_channels: int,
    activation_t=nn.SiLU,
    norm_t=T(nn.GroupNorm)(num_groups=1),
    conv_t=Conv,
    **kwargs,
) -> nn.Module:
    return nn.Sequential(
        norm_t(num_channels=in_channels),
        activation_t(),
        conv_t(dim=dim, in_channels=in_channels, **kwargs),
    )


def ResnetBlock(
    dim: int,
    in_channels: int,
    out_channels: int,
    kernel_size: int = 3,
    conv_block_t=ConvBlock,
    conv_t=Conv,
    **kwargs,
) -> nn.Module:
    ConvBlock = T(conv_block_t)(
        dim=dim, kernel_size=kernel_size, padding=(kernel_size - 1) // 2, **kwargs
    )
    Conv = T(conv_t)(dim=dim, kernel_size=1)

    conv_block = Sequential(
        ConvBlock(in_channels=in_channels, out_channels=out_channels),
        ConvBlock(in_channels=out_channels, out_channels=out_channels),
    )
    conv = nn.Identity()
    if in_channels != out_channels:
        conv = Conv(in_channels=in_channels, out_channels=out_channels)

    return Module([conv_block, conv], lambda x: conv_block(x) + conv(x))


class GRN(nn.Module):
    """GRN (Global Response Normalization) layer from ConvNextV2 generic to any dim"""

    def __init__(self, dim: int, channels: int):
        super().__init__()
        ones = (1,) * dim
        self.gamma = nn.Parameter(torch.zeros(1, channels, *ones))
        self.beta = nn.Parameter(torch.zeros(1, channels, *ones))
        self.norm_dims = [d + 2 for d in range(dim)]

    def forward(self, x: Tensor) -> Tensor:
        Gx = torch.norm(x, p=2, dim=self.norm_dims, keepdim=True)
        Nx = Gx / (Gx.mean(dim=1, keepdim=True) + 1e-6)
        return self.gamma * (x * Nx) + self.beta + x


def ConvNextV2Block(dim: int, channels: int) -> nn.Module:
    block = nn.Sequential(
        # Depthwise and LayerNorm
        Conv(
            dim=dim,
            in_channels=channels,
            out_channels=channels,
            kernel_size=7,
            padding=3,
            groups=channels,
        ),
        nn.GroupNorm(num_groups=1, num_channels=channels),
        # Pointwise expand
        Conv(dim=dim, in_channels=channels, out_channels=channels * 4, kernel_size=1),
        # Activation and GRN
        nn.GELU(),
        GRN(dim=dim, channels=channels * 4),
        # Pointwise contract
        Conv(
            dim=dim,
            in_channels=channels * 4,
            out_channels=channels,
            kernel_size=1,
        ),
    )

    return Module([block], lambda x: x + block(x))


def AttentionBase(features: int, head_features: int, num_heads: int) -> nn.Module:
    scale = head_features**-0.5
    mid_features = head_features * num_heads
    to_out = nn.Linear(in_features=mid_features, out_features=features, bias=False)

    def forward(
        q: Tensor, k: Tensor, v: Tensor, mask: Optional[Tensor] = None
    ) -> Tensor:
        h = num_heads
        # Split heads
        q, k, v = map(lambda t: rearrange(t, "b n (h d) -> b h n d", h=h), (q, k, v))
        # Compute similarity matrix and add eventual mask
        sim = einsum("... n d, ... m d -> ... n m", q, k) * scale
        # Get attention matrix with softmax
        attn = sim.softmax(dim=-1)
        # Compute values
        out = einsum("... n m, ... m d -> ... n d", attn, v)
        out = rearrange(out, "b h n d -> b n (h d)")
        return to_out(out)

    return Module([to_out], forward)


def LinearAttentionBase(features: int, head_features: int, num_heads: int) -> nn.Module:
    scale = head_features**-0.5
    mid_features = head_features * num_heads
    to_out = nn.Linear(in_features=mid_features, out_features=features, bias=False)

    def forward(q: Tensor, k: Tensor, v: Tensor) -> Tensor:
        h = num_heads
        # Split heads
        q, k, v = map(lambda t: rearrange(t, "b n (h d) -> b h n d", h=h), (q, k, v))
        # Softmax rows and cols
        q = q.softmax(dim=-1) * scale
        k = k.softmax(dim=-2)
        # Attend on channel dim
        attn = einsum("... n d, ... n c -> ... d c", k, v)
        out = einsum("... n d, ... d c -> ... n c", q, attn)
        out = rearrange(out, "b h n d -> b n (h d)")
        return to_out(out)

    return Module([to_out], forward)


def FixedEmbedding(max_length: int, features: int):
    embedding = nn.Embedding(max_length, features)

    def forward(x: Tensor) -> Tensor:
        batch_size, length, device = *x.shape[0:2], x.device
        assert_message = "Input sequence length must be <= max_length"
        assert length <= max_length, assert_message
        position = torch.arange(length, device=device)
        fixed_embedding = embedding(position)
        fixed_embedding = repeat(fixed_embedding, "n d -> b n d", b=batch_size)
        return fixed_embedding

    return Module([embedding], forward)


class Attention(nn.Module):
    def __init__(
        self,
        features: int,
        *,
        head_features: int,
        num_heads: int,
        context_features: Optional[int] = None,
        max_length: Optional[int] = None,
        attention_base_t=AttentionBase,
        positional_embedding_t=None,
    ):
        super().__init__()
        self.context_features = context_features
        self.use_positional_embedding = exists(positional_embedding_t)
        self.use_context = exists(context_features)
        mid_features = head_features * num_heads
        context_features = default(context_features, features)

        self.max_length = max_length
        if self.use_positional_embedding:
            assert exists(max_length)
            self.positional_embedding = positional_embedding_t(
                max_length=max_length, features=features
            )

        self.norm = nn.LayerNorm(features)
        self.norm_context = nn.LayerNorm(context_features)
        self.to_q = nn.Linear(
            in_features=features, out_features=mid_features, bias=False
        )
        self.to_kv = nn.Linear(
            in_features=context_features, out_features=mid_features * 2, bias=False
        )
        self.attention = attention_base_t(
            features, num_heads=num_heads, head_features=head_features
        )

    def forward(self, x: Tensor, context: Optional[Tensor] = None) -> Tensor:
        assert_message = "You must provide a context when using context_features"
        assert not self.context_features or exists(context), assert_message
        skip = x
        if self.use_positional_embedding:
            x = x + self.positional_embedding(x)
        # Use context if provided
        context = context if exists(context) and self.use_context else x
        # Normalize then compute q from input and k,v from context
        x, context = self.norm(x), self.norm_context(context)
        q, k, v = (self.to_q(x), *torch.chunk(self.to_kv(context), chunks=2, dim=-1))
        # Compute and return attention
        return skip + self.attention(q, k, v)


def CrossAttention(context_features: int, **kwargs):
    return Attention(context_features=context_features, **kwargs)


def FeedForward(features: int, multiplier: int) -> nn.Module:
    mid_features = features * multiplier
    return Skip(torch.add)(
        nn.Linear(in_features=features, out_features=mid_features),
        nn.GELU(),
        nn.Linear(in_features=mid_features, out_features=features),
    )


def Modulation(in_features: int, num_features: int) -> nn.Module:
    to_scale_shift = nn.Sequential(
        nn.SiLU(),
        nn.Linear(in_features=num_features, out_features=in_features * 2, bias=True),
    )
    norm = nn.LayerNorm(in_features, elementwise_affine=False, eps=1e-6)

    def forward(x: Tensor, features: Tensor) -> Tensor:
        scale_shift = to_scale_shift(features)
        scale, shift = rearrange(scale_shift, "b d -> b 1 d").chunk(2, dim=-1)
        return norm(x) * (1 + scale) + shift

    return Module([to_scale_shift, norm], forward)


def MergeAdd():
    return Module([], lambda x, y, *_: x + y)


def MergeCat(dim: int, channels: int, scale: float = 2**-0.5) -> nn.Module:
    conv = Conv(dim=dim, in_channels=channels * 2, out_channels=channels, kernel_size=1)
    return Module([conv], lambda x, y, *_: conv(torch.cat([x * scale, y], dim=1)))


def MergeModulate(dim: int, channels: int, modulation_features: int):
    to_scale = nn.Sequential(
        nn.SiLU(),
        nn.Linear(in_features=modulation_features, out_features=channels, bias=True),
    )

    def forward(x: Tensor, y: Tensor, features: Tensor, *args) -> Tensor:
        scale = rearrange(to_scale(features), f'b c -> b c {"1 " * dim}')
        return x + scale * y

    return Module([to_scale], forward)


"""
Embedders
"""


class NumberEmbedder(nn.Module):
    def __init__(self, features: int, dim: int = 256):
        super().__init__()
        assert dim % 2 == 0, f"dim must be divisible by 2, found {dim}"
        self.features = features
        self.weights = nn.Parameter(torch.randn(dim // 2))
        self.to_out = nn.Linear(in_features=dim + 1, out_features=features)

    def to_embedding(self, x: Tensor) -> Tensor:
        x = rearrange(x, "b -> b 1")
        freqs = x * rearrange(self.weights, "d -> 1 d") * 2 * pi
        fouriered = torch.cat((freqs.sin(), freqs.cos()), dim=-1)
        fouriered = torch.cat((x, fouriered), dim=-1)
        return self.to_out(fouriered)

    def forward(self, x: Union[Sequence[float], Tensor]) -> Tensor:
        if not torch.is_tensor(x):
            x = torch.tensor(x, device=self.weights.device)
        assert isinstance(x, Tensor)
        shape = x.shape
        x = rearrange(x, "... -> (...)")
        return self.to_embedding(x).view(*shape, self.features)  # type: ignore


class T5Embedder(nn.Module):
    def __init__(self, model: str = "t5-base", max_length: int = 64):
        super().__init__()
        from transformers import AutoTokenizer, T5EncoderModel

        self.tokenizer = AutoTokenizer.from_pretrained(model)
        self.transformer = T5EncoderModel.from_pretrained(model)
        self.max_length = max_length
        self.embedding_features = self.transformer.config.d_model

    @torch.no_grad()
    def forward(self, texts: Sequence[str]) -> Tensor:
        encoded = self.tokenizer(
            texts,
            truncation=True,
            max_length=self.max_length,
            padding="max_length",
            return_tensors="pt",
        )

        device = next(self.transformer.parameters()).device
        input_ids = encoded["input_ids"].to(device)
        attention_mask = encoded["attention_mask"].to(device)

        self.transformer.eval()

        embedding = self.transformer(
            input_ids=input_ids, attention_mask=attention_mask
        )["last_hidden_state"]

        return embedding


"""
Plugins
"""


def rand_bool(shape: Any, proba: float, device: Any = None) -> Tensor:
    if proba == 1:
        return torch.ones(shape, device=device, dtype=torch.bool)
    elif proba == 0:
        return torch.zeros(shape, device=device, dtype=torch.bool)
    else:
        return torch.bernoulli(torch.full(shape, proba, device=device)).to(torch.bool)


def ClassifierFreeGuidancePlugin(
    net_t: Type[nn.Module],
    embedding_max_length: int,
) -> Callable[..., nn.Module]:
    """Classifier-Free Guidance -> CFG(UNet, embedding_max_length=512)(...)"""

    def Net(embedding_features: int, **kwargs) -> nn.Module:
        fixed_embedding = FixedEmbedding(
            max_length=embedding_max_length,
            features=embedding_features,
        )
        net = net_t(embedding_features=embedding_features, **kwargs)  # type: ignore

        def forward(
            x: Tensor,
            embedding: Optional[Tensor] = None,
            embedding_scale: float = 1.0,
            embedding_mask_proba: float = 0.0,
            **kwargs,
        ):
            msg = "ClassiferFreeGuidancePlugin requires embedding"
            assert exists(embedding), msg
            b, device = embedding.shape[0], embedding.device
            embedding_mask = fixed_embedding(embedding)

            if embedding_mask_proba > 0.0:
                # Randomly mask embedding
                batch_mask = rand_bool(
                    shape=(b, 1, 1), proba=embedding_mask_proba, device=device
                )
                embedding = torch.where(batch_mask, embedding_mask, embedding)

            if embedding_scale != 1.0:
                # Compute both normal and fixed embedding outputs
                out = net(x, embedding=embedding, **kwargs)
                out_masked = net(x, embedding=embedding_mask, **kwargs)
                # Scale conditional output using classifier-free guidance
                return out_masked + (out - out_masked) * embedding_scale
            else:
                return net(x, embedding=embedding, **kwargs)

        return Module([fixed_embedding, net], forward)

    return Net


def TimeConditioningPlugin(
    net_t: Type[nn.Module],
    num_layers: int = 2,
) -> Callable[..., nn.Module]:
    """Adds time conditioning (e.g. for diffusion)"""

    def Net(modulation_features: Optional[int] = None, **kwargs) -> nn.Module:
        msg = "TimeConditioningPlugin requires modulation_features"
        assert exists(modulation_features), msg

        embedder = NumberEmbedder(features=modulation_features)
        mlp = Repeat(
            nn.Sequential(
                nn.Linear(modulation_features, modulation_features), nn.GELU()
            ),
            times=num_layers,
        )
        net = net_t(modulation_features=modulation_features, **kwargs)  # type: ignore

        def forward(
            x: Tensor,
            time: Optional[Tensor] = None,
            features: Optional[Tensor] = None,
            **kwargs,
        ):
            msg = "TimeConditioningPlugin requires time in forward"
            assert exists(time), msg
            # Process time to time_features
            time_features = F.gelu(embedder(time))
            time_features = mlp(time_features)
            # Overlap features if more than one per batch
            if time_features.ndim == 3:
                time_features = reduce(time_features, "b n d -> b d", "sum")
            # Merge time features with features if provided
            features = features + time_features if exists(features) else time_features
            return net(x, features=features, **kwargs)

        return Module([embedder, mlp, net], forward)

    return Net


def TextConditioningPlugin(
    net_t: Type[nn.Module], embedder: Optional[nn.Module] = None
) -> Callable[..., nn.Module]:
    """Adds text conditioning"""
    embedder = embedder if exists(embedder) else T5Embedder()
    msg = "TextConditioningPlugin embedder requires embedding_features attribute"
    assert hasattr(embedder, "embedding_features"), msg
    features: int = embedder.embedding_features  # type: ignore

    def Net(embedding_features: int = features, **kwargs) -> nn.Module:
        msg = f"TextConditioningPlugin requires embedding_features={features}"
        assert embedding_features == features, msg
        net = net_t(embedding_features=embedding_features, **kwargs)  # type: ignore

        def forward(
            x: Tensor, text: Sequence[str], embedding: Optional[Tensor] = None, **kwargs
        ):
            text_embedding = embedder(text)  # type: ignore
            if exists(embedding):
                text_embedding = torch.cat([text_embedding, embedding], dim=1)
            return net(x, embedding=text_embedding, **kwargs)

        return Module([embedder, net], forward)  # type: ignore

    return Net

def TabularDataClassifierFreeGuidancePlugin(
    net_t: Type[nn.Module],
    embedding_max_length: int,
) -> Callable[..., nn.Module]:
    """Classifier-Free Guidance -> CFG(UNet, embedding_max_length=512)(...)"""

    def Net(embedding_features: int, **kwargs) -> nn.Module:
        fixed_embedding = FixedEmbedding(
            max_length=embedding_max_length,
            features=embedding_features,
        )
        net = net_t(embedding_features=embedding_features, **kwargs)  # type: ignore

        def forward(
            x: Tensor,
            embedding: Optional[Tensor] = None,
            embedding_scale: float = 1.0,
            embedding_mask_proba: float = 0.0,
            **kwargs,
        ):
            msg = "ClassiferFreeGuidancePlugin requires embedding"
            assert exists(embedding), msg
            b, device = embedding.shape[0], embedding.device
            embedding_mask = fixed_embedding(embedding)

            if embedding_mask_proba > 0.0:
                # Randomly mask embedding
                batch_mask = rand_bool(
                    shape=(b, 1, 1), proba=embedding_mask_proba, device=device
                )
                embedding = torch.where(batch_mask, embedding_mask, embedding)

            if embedding_scale != 1.0:
                # Compute both normal and fixed embedding outputs
                out = net(x, embedding=embedding, **kwargs)
                out_masked = net(x, embedding=embedding_mask, **kwargs)
                # Scale conditional output using classifier-free guidance
                return out_masked + (out - out_masked) * embedding_scale
            else:
                return net(x, embedding=embedding, **kwargs)

        return Module([fixed_embedding, net], forward)

    return Net

def TabularDataConditioningPlugin(
    net_t: Type[nn.Module],
    cc_embedding_features: int,
    cat_dims: List[int],
    cat_idxs: List[int],
    cat_emb_dims: List[int],
    group_matrix: torch.Tensor,
) -> Callable[..., nn.Module]:
    embedder = TabularDataEmbeddingGenerator(cc_embedding_features, cat_dims, cat_idxs, cat_emb_dims, group_matrix)
    features: int = cc_embedding_features  # type: ignore

    def Net(embedding_features: int = features, **kwargs) -> nn.Module:
        msg = f"TextConditioningPlugin requires embedding_features={features}"
        assert embedding_features == features, msg
        net = net_t(embedding_features=embedding_features, **kwargs)  # type: ignore

        def forward(
            x: Tensor, ccData: Sequence[float], embedding: Optional[Tensor] = None, **kwargs
        ):  
            cc_embedding = embedder(ccData)  # type: ignore
            if exists(embedding):
                cc_embedding = torch.cat([cc_embedding, embedding], dim=1)
            return net(x, embedding=cc_embedding, **kwargs)

        return Module([embedder, net], forward)  # type: ignore

    return Net

#this is the embedding function for tabular data from tabnet https://github.com/dreamquark-ai/tabnet/blob/2c0c4ebd2bb1cb639ea94ab4b11823bc49265588/pytorch_tabnet/tab_network.py#L809
class TabularDataEmbeddingGenerator(torch.nn.Module):
    """
    Classical embeddings generator
    """

    def __init__(self, input_dim, cat_dims, cat_idxs, cat_emb_dims, group_matrix):
        """This is an embedding module for an entire set of features

        Parameters
        ----------
        input_dim : int
            Number of features coming as input (number of columns)
        cat_dims : list of int
            Number of modalities for each categorial features
            If the list is empty, no embeddings will be done
        cat_idxs : list of int
            Positional index for each categorical features in inputs
        cat_emb_dim : list of int
            Embedding dimension for each categorical features
            If int, the same embedding dimension will be used for all categorical features
        group_matrix : torch matrix
            Original group matrix before embeddings
        """
        super(TabularDataEmbeddingGenerator, self).__init__()

        if cat_dims == [] and cat_idxs == []:
            self.skip_embedding = True
            self.post_embed_dim = input_dim
            self.embedding_group_matrix = group_matrix.to(group_matrix.device)
            return
        else:
            self.skip_embedding = False

        self.post_embed_dim = int(input_dim + np.sum(cat_emb_dims) - len(cat_emb_dims))

        self.embeddings = torch.nn.ModuleList()

        for cat_dim, emb_dim in zip(cat_dims, cat_emb_dims):
            self.embeddings.append(torch.nn.Embedding(cat_dim, emb_dim))

        # record continuous indices
        self.continuous_idx = torch.ones(input_dim, dtype=torch.bool)
        self.continuous_idx[cat_idxs] = 0

        # update group matrix
        n_groups = group_matrix.shape[0]
        self.embedding_group_matrix = torch.empty((n_groups, self.post_embed_dim),
                                                  device=group_matrix.device)
        for group_idx in range(n_groups):
            post_emb_idx = 0
            cat_feat_counter = 0
            for init_feat_idx in range(input_dim):
                if self.continuous_idx[init_feat_idx] == 1:
                    # this means that no embedding is applied to this column
                    self.embedding_group_matrix[group_idx, post_emb_idx] = group_matrix[group_idx, init_feat_idx]  # noqa
                    post_emb_idx += 1
                else:
                    # this is a categorical feature which creates multiple embeddings
                    n_embeddings = cat_emb_dims[cat_feat_counter]
                    self.embedding_group_matrix[group_idx, post_emb_idx:post_emb_idx+n_embeddings] = group_matrix[group_idx, init_feat_idx] / n_embeddings  # noqa
                    post_emb_idx += n_embeddings
                    cat_feat_counter += 1

    def forward(self, x):
        """
        Apply embeddings to inputs
        Inputs should be (batch_size, input_dim)
        Outputs will be of size (batch_size, self.post_embed_dim)
        """
        if self.skip_embedding:
            # no embeddings required
            return x

        cols = []
        cat_feat_counter = 0
        for feat_init_idx, is_continuous in enumerate(self.continuous_idx):
            # Enumerate through continuous idx boolean mask to apply embeddings
            if is_continuous:
                cols.append(x[:, feat_init_idx].float().view(-1, 1))
            else:
                cols.append( 
                    self.embeddings[cat_feat_counter](x[:, feat_init_idx].long())
                )
                cat_feat_counter += 1
        # concat
        post_embeddings = torch.cat(cols, dim=1)
        return post_embeddings