import torch
from torch.testing import assert_close

import triton
import triton.language as tl

if torch.cuda.get_device_capability()[0] < 9:
    import sys
    print("Skipping TMA benchmark for GPU with compute capability < 9")
    sys.exit(0)


@triton.autotune(
    configs=[
        triton.Config({'BLOCK_SIZE_M': 128, 'BLOCK_SIZE_N': 128, 'BLOCK_SIZE_K': 64, 'GROUP_SIZE_M': 8}, num_stages=7, num_warps=4, enable_warp_specialization=False),
        # triton.Config({'BLOCK_SIZE_M': 128, 'BLOCK_SIZE_N': 128, 'BLOCK_SIZE_K': 64, 'GROUP_SIZE_M': 8}, num_stages=7, num_warps=4, enable_warp_specialization=True),
        # triton.Config({'BLOCK_SIZE_M': 256, 'BLOCK_SIZE_N': 128, 'BLOCK_SIZE_K': 64, 'GROUP_SIZE_M': 8}, num_stages=7, num_warps=4, num_ctas=2),
        # triton.Config({'BLOCK_SIZE_M': 512, 'BLOCK_SIZE_N': 128, 'BLOCK_SIZE_K': 64, 'GROUP_SIZE_M': 8}, num_stages=7, num_warps=4, num_ctas=4),
    ],
    key=['M', 'N', 'K'],
)
@triton.jit
def matmul_kernel_hopper(
    a_ptr, b_ptr, bias_ptr, z_ptr,
    M, N, K,
    stride_am, stride_ak,
    stride_bk, stride_bn,
    stride_cm, stride_cn,
    stride_zm, stride_zn,
    BLOCK_SIZE_M: tl.constexpr, BLOCK_SIZE_N: tl.constexpr, BLOCK_SIZE_K: tl.constexpr, GROUP_SIZE_M: tl.constexpr,
    ADD_MATRIX: tl.constexpr, BIAS: tl.constexpr,
    RELU: tl.constexpr,
    A_ORDER_0: tl.constexpr, A_ORDER_1: tl.constexpr,
    B_ORDER_0: tl.constexpr, B_ORDER_1: tl.constexpr
):
    pid = tl.program_id(axis=0)
    num_pid_n = tl.cdiv(N, BLOCK_SIZE_N)
    num_pid_m = tl.cdiv(M, BLOCK_SIZE_M)
    num_pid_in_group = GROUP_SIZE_M * num_pid_n
    group_id = pid // num_pid_in_group
    first_pid_m = group_id * GROUP_SIZE_M
    group_size_m = min(num_pid_m - first_pid_m, GROUP_SIZE_M)
    pid_m = first_pid_m + (pid % group_size_m)
    pid_n = (pid % num_pid_in_group) // group_size_m
    block_offset_m = pid_m * BLOCK_SIZE_M
    block_offset_n = pid_n * BLOCK_SIZE_N

    a_tile_ptr = tl.make_block_ptr(base=a_ptr, shape=(M, K), strides=(stride_am, stride_ak),
                                   offsets=(block_offset_m, 0), block_shape=(BLOCK_SIZE_M, BLOCK_SIZE_K), order=(A_ORDER_0, A_ORDER_1))
    b_tile_ptr = tl.make_block_ptr(base=b_ptr, shape=(K, N), strides=(stride_bk, stride_bn),
                                   offsets=(0, block_offset_n), block_shape=(BLOCK_SIZE_K, BLOCK_SIZE_N), order=(B_ORDER_0, B_ORDER_1))
    z_block_ptr = tl.make_block_ptr(base=z_ptr, shape=(M, N), strides=(stride_zm, stride_zn),
                                    offsets=(block_offset_m, block_offset_n), block_shape=(BLOCK_SIZE_M, BLOCK_SIZE_N), order=(1, 0))
    z = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_N), dtype=tl.float32)

    offs_m = block_offset_m + tl.arange(0, BLOCK_SIZE_M)
    offs_n = block_offset_n + tl.arange(0, BLOCK_SIZE_N)
    z_ptrs = z_ptr + offs_m[:, None] * stride_zm + offs_n[None, :] * stride_zn
    mask = (offs_m < M)[:, None] & (offs_n < N)[None, :]

    for k in range(0, K, BLOCK_SIZE_K):
        a = tl.load(a_tile_ptr)
        b = tl.load(b_tile_ptr)
        z += tl.dot(a, b)
        a_tile_ptr = tl.advance(a_tile_ptr, [0, BLOCK_SIZE_K])
        b_tile_ptr = tl.advance(b_tile_ptr, [BLOCK_SIZE_K, 0])

    if ADD_MATRIX:
        z += tl.load(z_ptrs, mask=mask).to(tl.float32)
    if BIAS:
        bias_mask = offs_m < M
        ZRs = bias_ptr + offs_m * stride_cm
        z += tl.load(ZRs, bias_mask)[:, None].to(tl.float32)
    if RELU:
        z = tl.maximum(z, 0)

    z = z.to(tl.float16)
    tl.store(z_block_ptr, z, boundary_check=(0, 1))


@triton.autotune(
    configs=[
        # triton.Config({'BLOCK_SIZE_M': 128, 'BLOCK_SIZE_N': 128, 'BLOCK_SIZE_K': 64, 'GROUP_SIZE_M': 8}, num_stages=6, num_warps=4, enable_warp_specialization=False),
        triton.Config({'BLOCK_SIZE_M': 128, 'BLOCK_SIZE_N': 128, 'BLOCK_SIZE_K': 64, 'GROUP_SIZE_M': 8}, num_stages=6, num_warps=4, enable_warp_specialization=True),
    ],
    key=['M', 'N', 'K'],
)
@triton.jit
def static_persistent_matmul_kernel_hopper(
    a_ptr, b_ptr, bias_ptr, z_ptr,
    M, N, K,
    stride_am, stride_ak,
    stride_bk, stride_bn,
    stride_cm, stride_cn,
    stride_zm, stride_zn,
    BLOCK_SIZE_M: tl.constexpr, BLOCK_SIZE_N: tl.constexpr, BLOCK_SIZE_K: tl.constexpr, GROUP_SIZE_M: tl.constexpr,
    ADD_MATRIX: tl.constexpr, BIAS: tl.constexpr,
    RELU: tl.constexpr,
    A_ORDER_0: tl.constexpr, A_ORDER_1: tl.constexpr,
    B_ORDER_0: tl.constexpr, B_ORDER_1: tl.constexpr,
    NUM_SM: tl.constexpr
):
    start_pid = tl.program_id(axis=0)
    num_pid_n = tl.cdiv(N, BLOCK_SIZE_N)
    num_pid_m = tl.cdiv(M, BLOCK_SIZE_M)
    num_tiles = num_pid_m * num_pid_n
    num_pid_in_group = GROUP_SIZE_M * num_pid_n
    group_id = start_pid // num_pid_in_group
    first_pid_m = group_id * GROUP_SIZE_M
    group_size_m = min(num_pid_m - first_pid_m, GROUP_SIZE_M)
    pre_pid_m = first_pid_m + ((start_pid % num_pid_in_group) % group_size_m)
    pre_pid_n = (start_pid % num_pid_in_group) // group_size_m

    pre_block_offset_m = pre_pid_m * BLOCK_SIZE_M
    pre_block_offset_n = pre_pid_n * BLOCK_SIZE_N
    a_tile_ptr = tl.make_block_ptr(base=a_ptr, shape=(M, K), strides=(stride_am, stride_ak),
                                   offsets=(pre_block_offset_m, 0), block_shape=(BLOCK_SIZE_M, BLOCK_SIZE_K), order=(A_ORDER_0, A_ORDER_1))
    b_tile_ptr = tl.make_block_ptr(base=b_ptr, shape=(K, N), strides=(stride_bk, stride_bn),
                                   offsets=(0, pre_block_offset_n), block_shape=(BLOCK_SIZE_K, BLOCK_SIZE_N), order=(B_ORDER_0, B_ORDER_1))
    z_block_ptr = tl.make_block_ptr(base=z_ptr, shape=(M, N), strides=(stride_zm, stride_zn),
                                    offsets=(pre_block_offset_m, pre_block_offset_n), block_shape=(BLOCK_SIZE_M, BLOCK_SIZE_N), order=(1, 0))

    for tile_id in range(start_pid, num_tiles, NUM_SM):
        group_id = tile_id // num_pid_in_group
        first_pid_m = group_id * GROUP_SIZE_M
        group_size_m = min(num_pid_m - first_pid_m, GROUP_SIZE_M)
        pid_m = first_pid_m + ((tile_id % num_pid_in_group) % group_size_m)
        pid_n = (tile_id % num_pid_in_group) // group_size_m
        block_offset_m = pid_m * BLOCK_SIZE_M
        block_offset_n = pid_n * BLOCK_SIZE_N

        a_tile_ptr = tl.advance(a_tile_ptr, [(pid_m - pre_pid_m) * BLOCK_SIZE_M, 0])
        b_tile_ptr = tl.advance(b_tile_ptr, [0, (pid_n - pre_pid_n) * BLOCK_SIZE_N])
        z = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_N), dtype=tl.float32)
        for k in range(0, K, BLOCK_SIZE_K):
            a = tl.load(a_tile_ptr, boundary_check=(0, 1))
            b = tl.load(b_tile_ptr, boundary_check=(0, 1))
            z += tl.dot(a, b)
            a_tile_ptr = tl.advance(a_tile_ptr, [0, BLOCK_SIZE_K])
            b_tile_ptr = tl.advance(b_tile_ptr, [BLOCK_SIZE_K, 0])
        a_tile_ptr = tl.advance(a_tile_ptr, [0, -tl.cdiv(K, BLOCK_SIZE_K) * BLOCK_SIZE_K])
        b_tile_ptr = tl.advance(b_tile_ptr, [-tl.cdiv(K, BLOCK_SIZE_K) * BLOCK_SIZE_K, 0])

        if ADD_MATRIX:
            offs_m = block_offset_m + tl.arange(0, BLOCK_SIZE_M)
            offs_n = block_offset_n + tl.arange(0, BLOCK_SIZE_N)
            z_ptrs = z_ptr + offs_m[:, None] * stride_zm + offs_n[None, :] * stride_zn
            mask = (offs_m < M)[:, None] & (offs_n < N)[None, :]
            z += tl.load(z_ptrs, mask=mask).to(tl.float32)
        if BIAS:
            bias_mask = offs_m < M
            ZRs = bias_ptr + offs_m * stride_cm
            z += tl.load(ZRs, bias_mask)[:, None].to(tl.float32)
        if RELU:
            z = tl.maximum(z, 0)

        z = z.to(tl.float16)
        z_block_ptr = tl.advance(z_block_ptr, [(pid_m - pre_pid_m) * BLOCK_SIZE_M, (pid_n - pre_pid_n) * BLOCK_SIZE_N])
        tl.store(z_block_ptr, z, boundary_check=(0, 1))

        pre_pid_m = pid_m
        pre_pid_n = pid_n


# `triton.jit`'ed functions can be auto-tuned by using the `triton.autotune` decorator, which consumes:
#   - A list of `triton.Config` objects that define different configurations of
#       meta-parameters (e.g., `BLOCK_SIZE_M`) and compilation options (e.g., `num_warps`) to try
#   - An auto-tuning *key* whose change in values will trigger evaluation of all the
#       provided configs
@triton.autotune(
    configs=[
        triton.Config({'BLOCK_SIZE_M': 128, 'BLOCK_SIZE_N': 256, 'BLOCK_SIZE_K': 64, 'GROUP_SIZE_M': 8}, num_stages=3, num_warps=8),
        triton.Config({'BLOCK_SIZE_M': 64, 'BLOCK_SIZE_N': 256, 'BLOCK_SIZE_K': 32, 'GROUP_SIZE_M': 8}, num_stages=4, num_warps=4),
        triton.Config({'BLOCK_SIZE_M': 128, 'BLOCK_SIZE_N': 128, 'BLOCK_SIZE_K': 32, 'GROUP_SIZE_M': 8}, num_stages=4, num_warps=4),
        triton.Config({'BLOCK_SIZE_M': 128, 'BLOCK_SIZE_N': 64, 'BLOCK_SIZE_K': 32, 'GROUP_SIZE_M': 8}, num_stages=4, num_warps=4),
        triton.Config({'BLOCK_SIZE_M': 64, 'BLOCK_SIZE_N': 128, 'BLOCK_SIZE_K': 32, 'GROUP_SIZE_M': 8}, num_stages=4, num_warps=4),
        triton.Config({'BLOCK_SIZE_M': 128, 'BLOCK_SIZE_N': 32, 'BLOCK_SIZE_K': 32, 'GROUP_SIZE_M': 8}, num_stages=4, num_warps=4),
        triton.Config({'BLOCK_SIZE_M': 64, 'BLOCK_SIZE_N': 32, 'BLOCK_SIZE_K': 32, 'GROUP_SIZE_M': 8}, num_stages=5, num_warps=2),
        triton.Config({'BLOCK_SIZE_M': 32, 'BLOCK_SIZE_N': 64, 'BLOCK_SIZE_K': 32, 'GROUP_SIZE_M': 8}, num_stages=5, num_warps=2),
    ],
    key=['M', 'N', 'K'],
)
@triton.jit
def matmul_kernel_ampere(
    # Pointers to matrices
    a_ptr, b_ptr, bias_ptr, c_ptr,
    # Matrix dimensions
    M, N, K,
    # The stride variables represent how much to increase the ptr by when moving by 1
    # element in a particular dimension. E.g. `stride_am` is how much to increase `a_ptr`
    # by to get the element one row down (A has M rows).
    stride_am, stride_ak,
    stride_bk, stride_bn,
    stride_biasm, stride_biasn,
    stride_cm, stride_cn,
    # Meta-parameters
    BLOCK_SIZE_M: tl.constexpr, BLOCK_SIZE_N: tl.constexpr, BLOCK_SIZE_K: tl.constexpr,
    GROUP_SIZE_M: tl.constexpr,
    ADD_MATRIX: tl.constexpr, BIAS: tl.constexpr,
    RELU: tl.constexpr,
):
    """Kernel for computing the matmul C = A x B.
    A has shape (M, K), B has shape (K, N) and C has shape (M, N)
    """
    # -----------------------------------------------------------
    # Map program ids `pid` to the block of C it should compute.
    # This is done in a grouped ordering to promote L2 data reuse.
    # See above `L2 Cache Optimizations` section for details.
    pid = tl.program_id(axis=0)
    num_pid_m = tl.cdiv(M, BLOCK_SIZE_M)
    num_pid_n = tl.cdiv(N, BLOCK_SIZE_N)
    num_pid_in_group = GROUP_SIZE_M * num_pid_n
    group_id = pid // num_pid_in_group
    first_pid_m = group_id * GROUP_SIZE_M
    group_size_m = min(num_pid_m - first_pid_m, GROUP_SIZE_M)
    pid_m = first_pid_m + (pid % group_size_m)
    pid_n = (pid % num_pid_in_group) // group_size_m

    # ----------------------------------------------------------
    # Create pointers for the first blocks of A and B.
    # We will advance this pointer as we move in the K direction
    # and accumulate
    # `a_ptrs` is a block of [BLOCK_SIZE_M, BLOCK_SIZE_K] pointers
    # `b_ptrs` is a block of [BLOCK_SIZE_K, BLOCK_SIZE_N] pointers
    # See above `Pointer Arithmetics` section for details
    offs_am = (pid_m * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)) % M
    offs_bn = (pid_n * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)) % N
    offs_k = tl.arange(0, BLOCK_SIZE_K)
    a_ptrs = a_ptr + (offs_am[:, None] * stride_am + offs_k[None, :] * stride_ak)
    b_ptrs = b_ptr + (offs_k[:, None] * stride_bk + offs_bn[None, :] * stride_bn)

    # -----------------------------------------------------------
    # Iterate to compute a block of the C matrix.
    # We accumulate into a `[BLOCK_SIZE_M, BLOCK_SIZE_N]` block
    # of fp32 values for higher accuracy.
    # `accumulator` will be converted back to fp16 after the loop.
    z = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_N), dtype=tl.float32)
    for k in range(0, tl.cdiv(K, BLOCK_SIZE_K)):
        # Load the next block of A and B, generate a mask by checking the K dimension.
        # If it is out of bounds, set it to 0.
        a = tl.load(a_ptrs, mask=offs_k[None, :] < K - k * BLOCK_SIZE_K, other=0.0)
        b = tl.load(b_ptrs, mask=offs_k[:, None] < K - k * BLOCK_SIZE_K, other=0.0)
        # We accumulate along the K dimension.
        z += tl.dot(a, b)
        # Advance the ptrs to the next K block.
        a_ptrs += BLOCK_SIZE_K * stride_ak
        b_ptrs += BLOCK_SIZE_K * stride_bk
    # -----------------------------------------------------------
    # Write back the block of the output matrix C with masks.
    offs_cm = pid_m * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)
    offs_cn = pid_n * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)
    c_ptrs = c_ptr + stride_cm * offs_cm[:, None] + stride_cn * offs_cn[None, :]
    c_mask = (offs_cm[:, None] < M) & (offs_cn[None, :] < N)

    if ADD_MATRIX:
        z += tl.load(c_ptrs, mask=c_mask).to(tl.float32)
    if BIAS:
        bias_mask = offs_cm < M
        ZRs = bias_ptr + offs_cm * stride_biasm
        z += tl.load(ZRs, bias_mask)[:, None].to(tl.float32)
    if RELU:
        z = tl.maximum(z, 0)

    # You can fuse arbitrary activation functions here
    # while the accumulator is still in FP32!
    z = z.to(tl.float16)
    tl.store(c_ptrs, z, mask=c_mask)


def matmul(a, b, a_order, b_order, bias, z, is_hopper, epilogue, persistent, num_sm):
    # checks constraints
    assert a.shape[1] == b.shape[0], "incompatible dimensions"
    assert epilogue in ['none', 'add-matrix', 'Bias', 'ReLu', 'ReLuBias'], "invalid epilogue"
    M, K = a.shape
    K, N = b.shape

    bias_stridem = bias.stride(0) if bias is not None else 0
    bias_striden = bias.stride(1) if bias is not None else 0

    def grid(META):
        return (triton.cdiv(M, META['BLOCK_SIZE_M']) * triton.cdiv(N, META['BLOCK_SIZE_N']),)
    if is_hopper:
        if persistent:
            def persistent_grid(META):
                return (min(META['NUM_SM'], triton.cdiv(M, META['BLOCK_SIZE_M']) * triton.cdiv(N, META['BLOCK_SIZE_N'])),)
            static_persistent_matmul_kernel_hopper[persistent_grid](a_ptr=a, b_ptr=b, bias_ptr=bias, z_ptr=z,
                                                                    M=M, N=N, K=K,
                                                                    stride_am=a.stride(0), stride_ak=a.stride(1),
                                                                    stride_bk=b.stride(0), stride_bn=b.stride(1),
                                                                    stride_cm=bias_stridem, stride_cn=bias_striden,
                                                                    stride_zm=z.stride(0), stride_zn=z.stride(1),
                                                                    ADD_MATRIX=epilogue == 'add-matrix',
                                                                    BIAS=epilogue in ['Bias', 'ReLuBias'],
                                                                    RELU=epilogue in ['ReLu', 'ReLuBias'],
                                                                    A_ORDER_0=a_order[0], A_ORDER_1=a_order[1],
                                                                    B_ORDER_0=b_order[0], B_ORDER_1=b_order[1],
                                                                    NUM_SM=num_sm)
        else:
            matmul_kernel_hopper[grid](a_ptr=a, b_ptr=b, bias_ptr=bias, z_ptr=z,
                                       M=M, N=N, K=K,
                                       stride_am=a.stride(0), stride_ak=a.stride(1),
                                       stride_bk=b.stride(0), stride_bn=b.stride(1),
                                       stride_cm=bias_stridem, stride_cn=bias_striden,
                                       stride_zm=z.stride(0), stride_zn=z.stride(1),
                                       ADD_MATRIX=epilogue == 'add-matrix',
                                       BIAS=epilogue in ['Bias', 'ReLuBias'],
                                       RELU=epilogue in ['ReLu', 'ReLuBias'],
                                       A_ORDER_0=a_order[0], A_ORDER_1=a_order[1],
                                       B_ORDER_0=b_order[0], B_ORDER_1=b_order[1],
                                       )
    else:
        assert persistent is False
        matmul_kernel_ampere[grid](
            a, b, bias, z,
            M, N, K,
            a.stride(0), a.stride(1),
            b.stride(0), b.stride(1),
            bias_stridem, bias_striden,
            z.stride(0), z.stride(1),
            ADD_MATRIX=epilogue == 'add-matrix',
            BIAS=epilogue in ['Bias', 'ReLuBias'],
            RELU=epilogue in ['ReLu', 'ReLuBias'],
        )

    return z


def problem_list(full=False):
    fast_test = [
        # [2048, 512, 512, False, True],
        # [2048, 1024, 1024, False, True],
        # [2048, 2048, 2048, False, True],
        # [2048, 4096, 4096, False, True],
        # [9352, 1528, 384, False, True],
        # [4024, 1208, 176, False, True],
        # [608, 112104, 16, True, True],
        # [9352, 1528, 384, False, True],
        # [15720, 1288, 144, False, False],
        [608, 112104, 16, True, True],
    ]
    full_test = [
        [40, 208, 720800, False, False],
        [16, 88, 51248, True, False],
        [80, 384, 163840, True, False],
        [352, 216, 229992, False, False],
        [56, 384, 115856, True, False],
        [112, 40, 34872, True, True],
        [24, 520, 975064, False, False],
        [416, 16, 121792, False, False],
        [32, 304, 86120, False, True],
        [360, 120, 69944, True, True],
        [80, 48, 25272, False, False],
        [440, 40, 142552, False, True],
        [216, 144, 168736, False, False],
        [224, 48, 55392, False, False],
        [368, 200, 134712, True, False],
        [504, 152, 155336, False, False],
        [96, 128, 18328, True, False],
        [472, 64, 52984, True, False],
        [136, 176, 21160, False, True],
        [240, 288, 64312, True, False],
        [120, 64, 12344, True, True],
        [32, 592, 82888, True, True],
        [336, 208, 100672, False, True],
        [104, 888, 46152, True, False],
        [432, 120, 40832, False, False],
        [32, 768, 55792, True, False],
        [616, 56, 22440, True, False],
        [104, 440, 32096, True, True],
        [256, 136, 36568, True, True],
        [88, 184, 11080, True, False],
        [768, 16, 75872, False, False],
        [64, 80, 9896, True, False],
        [160, 304, 19944, False, True],
        [104, 72, 8624, True, True],
        [112, 48, 9896, False, True],
        [24, 16, 8152, True, False],
        [184, 160, 21656, False, False],
        [104, 128, 9272, True, False],
        [944, 24, 59184, True, False],
        [192, 192, 16384, True, False],
        [56, 520, 33728, False, True],
        [376, 72, 29136, False, True],
        [472, 32, 18888, False, False],
        [80, 32, 9184, False, False],
        [192, 176, 22600, True, True],
        [528, 32, 25944, True, True],
        [72, 456, 16992, True, False],
        [352, 328, 78616, True, True],
        [336, 280, 37456, True, False],
        [48, 208, 10160, False, False],
        [160, 248, 25280, True, True],
        [24, 144, 9384, False, False],
        [312, 176, 14392, True, True],
        [368, 344, 26552, False, False],
        [376, 392, 77432, False, True],
        [224, 368, 28144, True, False],
        [112, 280, 12440, True, False],
        [112, 424, 13448, False, False],
        [200, 640, 137888, True, False],
        [96, 1000, 344720, True, True],
        [872, 88, 25560, True, True],
        [696, 16, 24632, True, False],
        [72, 640, 30024, False, False],
        [360, 336, 22480, True, False],
        [336, 512, 95144, True, False],
        [96, 192, 11912, False, False],
        [160, 544, 203536, True, True],
        [296, 144, 15568, True, True],
        [656, 88, 18632, True, False],
        [112, 1256, 37328, True, False],
        [64, 288, 10792, True, True],
        [48, 328, 10544, False, True],
        [136, 600, 15256, True, False],
        [96, 264, 8648, False, False],
        [360, 336, 17224, True, True],
        [24, 888, 41176, False, True],
        [272, 184, 19536, False, True],
        [1440, 24, 98072, True, False],
        [336, 40, 7664, False, False],
        [384, 200, 12632, False, False],
        [504, 288, 33488, True, True],
        [72, 608, 22832, False, False],
        [104, 56, 6792, False, True],
        [528, 184, 33768, True, True],
        [312, 96, 10056, True, False],
        [360, 256, 11736, True, True],
        [688, 224, 31320, False, True],
        [56, 152, 7104, False, True],
        [136, 80, 6216, False, True],
        [336, 24, 6640, True, False],
        [120, 560, 12912, True, True],
        [536, 56, 8440, True, False],
        [216, 24, 6320, False, True],
        [272, 488, 21952, False, False],
        [200, 40, 5456, False, False],
        [56, 1088, 24472, True, True],
        [56, 88, 3896, True, False],
        [72, 424, 9840, False, True],
        [256, 344, 11096, False, True],
        [56, 64, 6360, False, True],
        [48, 648, 13120, False, False],
        [592, 64, 12720, True, True],
        [96, 272, 6120, False, True],
        [336, 152, 7792, True, False],
        [840, 56, 13192, False, True],
        [1192, 96, 58864, False, False],
        [48, 1232, 55056, False, True],
        [1288, 72, 30440, True, True],
        [208, 688, 45560, True, False],
        [1448, 56, 65552, False, False],
        [272, 240, 9784, True, True],
        [136, 640, 23848, False, False],
        [40, 544, 7824, False, False],
        [272, 328, 11728, False, False],
        [392, 40, 3912, False, False],
        [520, 184, 12832, True, False],
        [1000, 152, 49768, False, True],
        [552, 48, 6536, False, True],
        [312, 456, 14352, True, False],
        [48, 56, 3768, True, False],
        [320, 472, 26392, True, True],
        [120, 424, 5224, False, False],
        [656, 280, 125144, True, True],
        [400, 160, 7560, True, False],
        [648, 32, 7080, False, False],
        [16, 524288, 32, False, True],
        [456, 136, 23600, False, True],
        [104, 168, 4232, False, True],
        [176, 504, 9480, True, True],
        [496, 152, 6584, False, False],
        [720, 288, 36768, True, False],
        [1136, 24, 20504, True, True],
        [96, 88, 3960, False, True],
        [96, 472, 5296, False, False],
        [176, 40, 4288, False, False],
        [128, 256, 6912, True, True],
        [512, 16, 6912, True, True],
        [16, 256, 6912, True, True],
        [160, 1040, 31800, False, False],
        [784, 136, 29216, True, True],
        [64, 984, 13904, True, False],
        [688, 376, 116936, False, False],
        [16, 104, 4320, False, True],
        [416, 224, 15536, False, False],
        [168, 40, 3848, False, True],
        [240, 464, 13056, False, False],
        [216, 816, 36864, False, False],
        [112, 104, 3384, False, True],
        [224, 920, 29544, True, True],
        [544, 104, 7904, False, True],
        [296, 80, 4488, False, True],
        [24, 496, 4584, True, False],
        [1680, 112, 282672, False, False],
        [64, 1936, 46928, False, False],
        [224, 600, 123024, True, True],
        [160, 856, 19128, True, False],
        [96, 1736, 38688, True, False],
        [224, 296, 4648, False, False],
        [1520, 112, 102768, False, True],
        [8, 368, 5064, True, False],
        [160, 360, 4896, False, False],
        [152, 56, 3432, False, True],
        [48, 1088, 16600, False, True],
        [1064, 64, 16168, False, True],
        [176, 360, 3952, True, False],
        [208, 552, 9320, False, True],
        [328, 112, 5088, True, True],
        [1056, 64, 14568, True, True],
        [240, 584, 8904, False, True],
        [960, 40, 11472, False, True],
        [944, 48, 7888, True, False],
        [704, 328, 288600, False, False],
        [144, 240, 4992, True, True],
        [80, 992, 10168, True, False],
        [1592, 104, 10376, True, False],
        [880, 136, 9744, True, False],
        [24, 1320, 21136, False, True],
        [1600, 56, 22408, True, True],
        [48, 1048, 14192, True, True],
        [16, 1792, 24496, False, False],
        [416, 352, 6312, False, True],
        [152, 80, 3856, False, False],
        [80, 1952, 28072, True, False],
        [536, 480, 61096, True, True],
        [592, 512, 23048, True, False],
        [128, 1728, 22136, False, True],
        [72, 2336, 28832, True, False],
        [200, 520, 10272, True, True],
        [32, 480, 3736, False, False],
        [280, 224, 4816, True, False],
        [912, 96, 6504, False, True],
        [768, 240, 9592, True, False],
        [128, 936, 14112, True, True],
        [840, 16, 6912, True, True],
        [112, 1872, 93520, True, True],
        [392, 40, 3240, False, True],
        [240, 1320, 61320, True, False],
        [16, 976, 7304, True, False],
        [960, 120, 7592, True, False],
        [360, 104, 2280, True, True],
        [48, 1928, 20392, True, True],
        [40, 1648, 16120, False, False],
        [112, 1472, 23648, True, True],
        [520, 248, 5360, False, False],
        [1000, 216, 7208, False, False],
        [1096, 16, 5720, True, False],
        [640, 24, 3184, False, False],
        [360, 16, 3240, False, False],
        [352, 96, 2768, True, False],
        [1032, 144, 21280, True, False],
        [96, 1128, 5744, False, False],
        [136, 2280, 30736, True, False],
        [616, 208, 4344, True, False],
        [192, 1488, 59440, True, False],
        [1768, 80, 25680, False, False],
        [696, 432, 96608, True, False],
        [152, 1016, 4744, False, False],
        [1024, 336, 47400, False, False],
        [32, 144, 2320, False, False],
        [544, 16, 3272, False, True],
        [560, 464, 14280, False, True],
        [264, 360, 6800, False, True],
        [48, 3240, 148696, True, True],
        [88, 2352, 30616, False, True],
        [824, 136, 5384, False, False],
        [672, 24, 3296, True, True],
        [1432, 240, 21064, True, True],
        [96, 2360, 46360, True, True],
        [16, 1664, 15408, False, True],
        [816, 96, 6392, True, True],
        [296, 368, 3944, False, True],
        [256, 512, 6912, True, True],
        [256, 1128, 7000, False, False],
        [424, 424, 7472, True, False],
        [304, 800, 794496, True, True],
        [728, 216, 3432, True, False],
        [216, 952, 6320, True, True],
        [280, 360, 1784, False, False],
        [368, 200, 4848, True, True],
        [320, 400, 4848, False, False],
        [40, 56, 1656, True, False],
        [2416, 32, 31392, False, False],
        [824, 328, 6344, True, False],
        [264, 136, 664, False, False],
        [96, 528, 3440, False, False],
        [24, 1248, 5448, False, False],
        [1440, 56, 5384, False, True],
        [416, 328, 3992, False, True],
        [2312, 96, 464720, False, False],
        [2672, 48, 28632, False, False],
        [32, 992, 4032, False, False],
        [1200, 224, 58112, False, True],
        [984, 336, 9760, True, False],
        [2632, 16, 19848, True, False],
        [24, 3624, 82384, True, True],
        [64, 392, 2576, True, True],
        [248, 344, 2256, False, True],
        [432, 96, 2376, False, True],
        [24, 488, 1216, False, False],
        [120, 1808, 625184, False, True],
        [432, 568, 11712, False, True],
        [1496, 144, 19176, False, False],
        [1088, 64, 3976, False, True],
        [608, 640, 109224, True, False],
        [280, 784, 8656, True, False],
        [200, 800, 5680, True, True],
        [112, 1880, 21536, True, True],
        [864, 96, 4248, True, True],
        [240, 1016, 5544, False, True],
        [72, 384, 2136, True, True],
        [1256, 304, 15000, True, False],
        [496, 232, 2416, True, False],
        [192, 896, 8656, False, False],
        [328, 200, 2704, False, False],
        [96, 152, 1032, False, True],
        [728, 464, 16688, False, False],
        [80, 24, 944, False, False],
        [1024, 96, 4096, True, True],
        [432, 552, 7008, True, False],
        [608, 112104, 16, True, True],
        [88, 2240, 33536, False, True],
        [920, 312, 22512, True, True],
        [384, 216, 2440, True, False],
        [472, 520, 4856, True, False],
        [672, 152, 3288, False, False],
        [1496, 176, 9232, True, True],
        [1800, 56, 4744, False, False],
        [288, 480, 3168, True, False],
        [376, 712, 5936, True, False],
        [136, 1640, 157824, False, True],
        [736, 280, 18624, False, True],
        [56, 440, 792, False, True],
        [192, 984, 9880, True, True],
        [272, 272, 3760, True, True],
        [840, 408, 9768, True, False],
        [152, 688, 3568, False, False],
        [24, 3696, 50320, False, True],
        [520, 728, 90344, True, True],
        [1024, 128, 4096, True, True],
        [72, 512, 2680, False, False],
        [248, 944, 8240, False, False],
        [192, 1456, 11296, True, False],
        [1672, 184, 58392, False, True],
        [528, 648, 19168, True, False],
        [264, 400, 2688, True, False],
        [96, 3000, 22416, True, False],
        [112, 1128, 5168, True, True],
        [4160, 56, 20152, True, False],
        [1360, 248, 7840, True, False],
        [512, 16, 2240, True, True],
        [904, 104, 968, False, False],
        [56, 32, 1032, False, False],
        [192, 1760, 22584, False, False],
        [32, 1200, 3392, True, True],
        [280, 1384, 51224, False, False],
        [224, 440, 2120, True, False],
        [2120, 144, 13752, True, True],
        [168, 1704, 54352, False, False],
        [1264, 88, 6032, False, True],
        [1696, 32, 6840, True, True],
        [568, 872, 208520, True, False],
        [544, 872, 11408, True, False],
        [40, 3736, 12920, True, False],
        [48, 1536, 3752, True, True],
        [192, 577600, 32, False, True],
        [312, 704, 7720, False, False],
        [80, 1416, 4848, False, False],
        [448, 264, 3696, False, True],
        [504, 280, 1976, False, False],
        [432, 904, 33816, True, False],
        [288, 48, 1128, False, True],
        [40, 1112, 736, True, False],
        [128, 256, 2240, True, True],
        [416, 344, 3344, False, True],
        [416, 680, 76624, False, True],
        [840, 136, 2760, False, False],
        [4200, 64, 35040, False, True],
        [24, 4392, 19304, False, True],
        [832, 632, 76008, True, True],
        [208, 1288, 7320, True, True],
        [704, 376, 3656, False, True],
        [944, 472, 278360, True, True],
        [72, 40, 96, True, False],
        [216, 1992, 46656, True, True],
        [16, 256, 2240, True, True],
        [32, 1496, 880, False, True],
        [144, 802816, 24, True, True],
        [3784, 16, 28304, False, False],
        [136, 1040, 4864, True, True],
        [224, 904, 584, False, False],
        [1000, 32, 1792, True, True],
        [128, 1832, 3000, False, False],
        [688, 96, 2688, True, False],
        [272, 208, 1768, True, False],
        [192, 1512, 2896, False, False],
        [48, 1296, 3760, False, True],
        [104, 120, 536, True, True],
        [1384, 336, 53704, True, True],
        [584, 152, 88, False, True],
        [712, 328, 3408, False, False],
        [24, 16, 16, False, True],
        [1024, 192, 4096, True, True],
        [712, 544, 27840, False, False],
        [680, 696, 32000, True, True],
        [632, 608, 76480, False, True],
        [16, 1920, 5704, False, True],
        [824, 112, 1504, True, False],
        [152, 248, 240, True, True],
        [40, 2448, 14304, True, True],
        [648, 264, 2336, True, True],
        [128, 600, 336, True, False],
        [3208, 112, 25920, False, False],
        [600, 840, 24568, False, True],
        [1784, 24, 680, False, True],
        [136, 296, 224, True, True],
        [120, 1680, 1512, True, True],
        [32, 1192, 336, True, False],
        [848, 520, 187296, True, False],
        [1056, 168, 1464, False, False],
        [288, 88, 80, False, True],
        [1864, 32, 3184, True, False],
        [144, 1176, 8656, False, True],
        [352, 472, 1104, False, True],
        [400, 216, 688, False, True],
        [512, 221184, 16, True, True],
        [160, 168, 880, False, True],
        [96, 1816, 832, False, False],
        [576, 72, 1336, True, True],
        [56, 1752, 1160, True, True],
        [96, 3928, 308488, True, False],
        [552, 568, 22704, False, True],
        [456, 304, 1808, False, False],
        [256, 512, 2240, True, True],
        [256, 512, 2240, True, True],
        [912, 656, 234296, True, True],
        [1488, 328, 20664, False, True],
        [32, 2776, 5496, False, False],
        [1408, 120, 3680, True, True],
        [15720, 1288, 144, False, False],
        [760, 24, 736, True, True],
        [408, 1016, 7376, False, False],
        [56, 144, 272, False, True],
        [96, 192, 408, False, False],
        [136, 688, 1744, False, False],
        [280, 600, 1040, True, True],
        [336, 112, 744, False, True],
        [160, 528, 1376, False, False],
        [96, 1048544, 16, True, True],
        [1432, 192, 5568, True, False],
        [1112, 200, 600, True, False],
        [216, 1944, 14808, False, False],
        [168, 728, 2640, False, True],
        [808, 480, 5200, True, False],
        [104, 1280, 936, False, True],
        [480, 488, 1176, True, True],
        [64, 1984, 1856, True, False],
        [1520, 352, 156864, True, True],
        [408, 1048, 6480, True, False],
        [256, 1168, 5872, True, True],
        [160, 992, 2008, True, True],
        [528, 544, 1728, False, False],
        [240, 872, 280, False, False],
        [824, 576, 9864, False, True],
        [3544, 112, 12664, False, True],
        [512, 56000, 16, True, True],
        [432, 432, 968, False, True],
        [56, 168, 144, False, True],
        [56, 2048, 2144, False, True],
        [184, 384, 816, True, True],
        [448, 688, 968, True, False],
        [80, 1984, 2272, True, False],
        [32, 5592, 6280, False, False],
        [136, 1032, 496, False, True],
        [440, 480, 1952, True, True],
        [2376, 56, 1768, False, False],
        [600, 632, 9408, False, True],
        [992, 280, 2520, True, True],
        [520, 584, 1736, False, False],
        [448, 488, 288, True, True],
        [3168, 24, 2584, False, False],
        [1072, 168, 40, True, False],
        [200, 1328, 3080, True, True],
        [80, 1720, 3184, False, False],
        [256, 2176, 18888, False, False],
        [4096, 1, 1024, False, True],
        [464, 96, 1120, True, True],
        [1216, 312, 4384, True, True],
        [360, 1520, 5624, True, True],
        [560, 536, 4488, True, True],
        [96, 524288, 16, False, True],
        [1232, 296, 122544, True, False],
        [72, 4328, 552, True, False],
        [872, 448, 25744, False, True],
        [1000, 256, 1280, True, True],
        [40, 768, 488, False, True],
        [320, 656, 1088, False, True],
        [440, 544, 2176, True, True],
        [1088, 480, 5368, True, False],
        [400, 1456, 80624, False, False],
        [296, 1024, 752, False, True],
        [128, 4264, 32, False, True],
        [1792, 32, 1000, False, True],
        [120, 400, 136, True, False],
        [216, 2440, 32, False, False],
        [16, 5512, 9144, True, True],
        [224, 432, 624, True, True],
        [16, 221184, 256, True, True],
        [544, 64, 96, True, False],
        [448, 1064, 216, False, True],
        [40, 1832, 1640, False, True],
        [328, 72, 704, False, False],
        [128, 3952, 20744, False, True],
        [152, 1152, 1008, True, False],
        [800, 408, 2200, False, False],
        [208, 1664, 976, True, False],
        [136, 656, 768, True, True],
        [14352, 992, 32, True, False],
        [152, 9400, 152, True, True],
        [1256, 296, 544, True, False],
        [888, 624, 44144, True, True],
        [4416, 152, 10440, False, False],
        [936, 712, 8616, False, True],
        [392, 848, 4144, True, False],
        [536, 576, 5688, False, False],
        [920, 384, 5232, False, True],
        [1080, 280, 504, True, True],
        [256, 2240, 16, True, True],
        [2504, 8656, 160, False, False],
        [384, 1360, 4400, True, False],
        [440, 1448, 37432, True, True],
        [256, 294912, 64, False, True],
        [88, 3032, 808, True, True],
        [632, 480, 1056, True, True],
        [64, 3944, 408, False, True],
        [16, 520, 216, True, True],
        [1280, 256, 1000, False, True],
        [2712, 176, 29976, False, False],
        [1560, 168, 968, False, False],
        [512, 1024, 6912, True, True],
        [1776, 64, 2224, False, True],
        [9240, 40, 248, True, False],
        [728, 608, 16, False, True],
        [160, 1008, 424, True, False],
        [1088, 416, 3320, True, False],
        [624, 464, 6176, False, True],
        [304, 1408, 31792, True, True],
        [648, 680, 1592, True, False],
        [864, 240, 72, True, False],
        [6216, 24, 1704, False, True],
        [120, 4856, 97768, False, True],
        [32, 3616, 560, False, False],
        [720, 584, 4608, False, True],
        [440, 1880, 24, False, True],
        [2288, 256, 12864, True, False],
        [128, 2760, 2056, False, True],
        [376, 672, 352, True, True],
        [224, 1560, 1144, True, True],
        [1968, 360, 9352, False, False],
        [152, 1864, 3976, True, True],
        [13040, 192, 64, True, False],
        [80, 2016, 536, True, True],
        [8048, 40, 3072, True, False],
        [13520, 3384, 328, False, False],
        [101264, 888, 280, False, False],
        [4024, 1208, 176, False, True],
        [72, 6776, 44016, True, False],
        [96, 3768, 824, True, True],
        [3080, 6320, 88, False, True],
        [760, 760, 8272, True, True],
        [552, 824, 1648, False, False],
        [96, 5368, 400, False, True],
        [3040, 32, 5472, True, True],
        [464, 976, 7152, False, True],
        [392, 1168, 3976, True, False],
        [1328, 480, 39080, False, True],
        [56, 10832, 3856, True, False],
        [2320, 64, 648, False, True],
        [816, 872, 400, True, False],
        [376, 229144, 280, True, False],
        [552, 592, 176, False, True],
        [1088, 584, 26264, False, True],
        [16, 6912, 256, True, True],
        [440, 864, 752, True, True],
        [1600, 296, 6976, True, False],
        [101896, 784, 136, True, True],
        [272, 3480, 16808, False, False],
        [2224, 152, 2440, False, True],
        [1544, 224, 1000, True, True],
        [2576, 168, 1792, True, True],
        [256, 409600, 64, True, True],
        [72, 5392, 12704, True, True],
        [976, 568, 3376, True, True],
        [88, 2960, 1624, True, True],
        [2360, 144, 3480, True, False],
        [392, 2544, 42552, True, True],
        [10696, 40, 3248, False, False],
        [1744, 208, 3880, False, False],
        [784, 1016, 192, True, False],
        [512, 200704, 128, False, True],
        [312, 1664, 288, False, True],
        [1432, 256, 2032, True, False],
        [16, 2240, 256, True, True],
        [48, 4240, 2080, False, True],
        [640, 688, 3528, True, True],
        [256, 802816, 64, True, True],
        [16, 7296, 34544, False, False],
        [928, 1264, 39880, False, True],
        [696, 2248, 157896, False, True],
        [896, 3504, 4264, False, True],
        [1216, 480, 944, True, False],
        [544, 944, 10056, False, False],
        [256, 802816, 64, True, True],
        [1024, 25600, 256, True, True],
        [4128, 2968, 360, True, True],
        [312, 1760, 1864, True, False],
        [1544, 1800, 20152, False, True],
        [1112, 664, 93288, True, True],
        [8664, 240, 8360, True, True],
        [520, 1576, 37992, False, True],
        [1816, 288, 36424, False, False],
        [696, 464, 4992, True, True],
        [5128, 408, 400, False, True],
        [1024, 512, 12544, False, True],
        [1024, 512, 12544, False, True],
        [3016, 800, 89016, True, True],
        [512, 6912, 16, True, True],
        [1704, 240, 800, False, False],
        [1920, 208, 1248, True, True],
        [256, 204800, 64, True, True],
        [512, 1024, 2240, True, True],
        [3096, 936, 1792, False, False],
        [1024, 480, 2240, True, True],
        [288, 1544, 3472, False, True],
        [88, 10632, 14040, True, False],
        [2024, 416, 27880, True, True],
        [112, 3512, 1776, False, True],
        [128, 2528, 1432, True, True],
        [136, 4520, 1176, False, False],
        [440, 1152, 1328, True, True],
        [1192, 488, 6392, False, False],
        [1824, 248, 1968, True, False],
        [13816, 64, 4776, True, False],
        [224, 131072, 224, True, True],
        [944, 704, 15424, False, False],
        [376, 2352, 79800, False, True],
        [2496, 568, 37320, False, True],
        [616, 4592, 1928, True, True],
        [712, 4536, 2080, True, False],
        [952, 1632, 38712, False, True],
        [3032, 392, 464, True, True],
        [392, 4480, 7880, True, True],
        [2040, 280, 1728, True, False],
        [520, 1752, 254816, False, False],
        [176, 4760, 104, True, True],
        [56, 8176, 6000, True, True],
        [4096, 128, 1024, True, True],
        [344, 1504, 4000, True, True],
        [304, 2256, 2432, False, False],
        [72, 3440, 2752, False, True],
        [16, 56000, 256, True, True],
        [3592, 336, 22056, False, True],
        [3400, 1464, 848, False, False],
        [208, 1784, 3552, True, True],
        [128, 2240, 256, True, True],
        [7728, 144, 93424, True, True],
        [104, 3680, 9992, False, True],
        [408, 2008, 184464, True, True],
        [6864, 248, 11496, False, True],
        [912, 2160, 180856, True, False],
        [256, 2240, 512, True, True],
        [968, 408, 3720, True, False],
        [8488, 144, 6032, False, False],
        [3072, 128, 4096, False, True],
        [592, 1072, 421040, True, True],
        [272, 4024, 13840, True, False],
        [1856, 2280, 696, True, False],
        [1224, 2088, 528, False, True],
        [80, 11808, 28816, False, False],
        [4096, 96, 1024, True, True],
        [832, 592, 5280, True, True],
        [24, 26640, 32760, True, True],
        [432, 1680, 1792, True, False],
        [1296, 480, 2608, True, True],
        [512, 1160, 2704, True, True],
        [3024, 10704, 344, False, False],
        [480, 1296, 328, False, True],
        [3912, 2264, 704, True, False],
        [696, 944, 1856, False, True],
        [1408, 424, 3328, False, True],
        [176, 2096, 248, False, True],
        [72, 6504, 8848, True, False],
        [32, 19472, 4696, True, True],
        [6680, 1880, 544, False, True],
        [536, 2704, 24, False, True],
        [1768, 240, 984, False, False],
        [960, 616, 3384, True, True],
        [1024, 50176, 256, True, True],
        [920, 2120, 152120, True, False],
        [896, 400, 4344, True, True],
        [544, 4776, 14232, False, False],
        [128, 6912, 256, True, True],
        [728, 12864, 184, True, True],
        [4760, 4144, 320, False, False],
        [520, 784, 304, False, True],
        [24, 16944, 16664, False, False],
        [7328, 272, 113544, False, True],
        [1024, 2240, 480, True, True],
        [560, 5080, 4792, True, False],
        [2016, 272, 294048, True, True],
        [160, 10072, 7968, False, False],
        [456, 1848, 37336, True, True],
        [184, 3120, 6776, True, True],
        [4096, 128, 1024, False, True],
        [240, 3768, 90552, False, False],
        [1688, 376, 1688, False, True],
        [4096, 3072, 128, True, False],
        [184, 3720, 2824, False, True],
        [24, 7232, 3800, True, True],
        [456, 2264, 93448, True, False],
        [1928, 328, 146672, False, True],
        [13624, 160, 2640, False, False],
        [5304, 96, 632, True, False],
        [49696, 152, 2064, True, True],
        [4832, 696, 968, True, True],
        [3664, 328, 54104, True, True],
        [3000, 272, 17104, True, True],
        [6776, 344, 15048, True, True],
        [9352, 1528, 384, False, True],
        [816, 1872, 6968, True, False],
        [1320, 504, 19792, False, True],
        [144, 6096, 2736, False, True],
        [11112, 120, 1896, True, True],
        [1176, 624, 221568, True, True],
        [512, 2240, 16, True, True],
        [1024, 50176, 256, False, True],
        [288, 131072, 96, True, True],
        [936, 1352, 2408, True, False],
        [4864, 16, 160, True, True],
        [672, 1432, 192, False, False],
        [1024, 6912, 480, True, True],
        [7584, 624, 23240, False, True],
        [88, 4576, 2256, True, False],
        [1744, 1560, 6608, True, False],
        [832, 18568, 504, False, False],
        [152, 50824, 2536, True, True],
        [2328, 240, 2136, True, False],
        [1336, 3480, 74968, False, False],
        [472, 5616, 85048, False, True],
        [1296, 464, 16432, False, True],
        [872, 816, 31552, True, True],
        [96, 8152, 8296, True, False],
        [480, 1480, 43904, True, True],
        [440, 2040, 104, True, True],
        [584, 16280, 1136, False, False],
        [1928, 1080, 2144, False, True],
        [976, 1336, 9120, False, False],
        [816, 728, 8312, True, False],
        [1520, 2192, 11896, False, False],
        [4984, 160, 13648, True, False],
        [6432, 184, 96, True, True],
        [3200, 864, 2408, False, True],
        [240, 2496, 11824, True, True],
        [1192, 944, 528, False, True],
        [448, 2648, 134376, True, False],
        [512, 160000, 256, False, True],
        [288, 7496, 680, False, True],
        [192, 16384, 192, False, True],
        [2808, 400, 58568, False, False],
        [928, 1336, 12552, False, True],
        [752, 3608, 27944, False, True],
        [2016, 536, 4744, False, True],
        [800, 896, 2960, False, True],
        [4840, 560, 5360, True, True],
        [400, 2864, 7152, True, False],
        [1144, 2392, 576, True, True],
        [4400, 312, 6800, True, False],
        [2184, 360, 17112, False, True],
        [2096, 360, 1144, False, True],
        [2304, 1416, 44488, True, False],
        [1024, 2240, 512, True, True],
        [2744, 248, 14120, False, True],
        [31624, 1200, 464, True, False],
        [4352, 2448, 424, False, False],
        [1488, 264, 8136, True, True],
        [176, 6888, 9304, False, True],
        [808, 1960, 432, True, True],
        [288, 139608, 960, False, True],
        [10152, 48, 248, False, True],
        [1384, 18512, 376, True, True],
        [96, 5560, 538888, True, False],
        [136, 2320, 2864, True, False],
        [664, 9152, 560, True, False],
        [256, 56000, 512, True, True],
        [440, 1264, 2816, True, True],
        [9112, 248, 1256, True, False],
        [400, 3112, 29616, True, True],
        [128, 56000, 256, True, True],
        [96, 9560, 18944, False, False],
        [8664, 104, 16992, False, False],
        [6552, 448, 2144, True, False],
        [8616, 488, 9832, True, False],
        [400, 4416, 10024, True, True],
        [248, 2568, 2696, False, True],
        [512, 102400, 128, True, True],
        [10520, 208, 22096, True, True],
        [1000, 3136, 55944, False, True],
        [1648, 680, 85736, True, False],
        [856, 1640, 60800, True, False],
        [72, 16992, 26912, True, True],
        [1832, 408, 42088, True, False],
        [472, 3096, 6784, False, False],
        [96, 10520, 62824, False, True],
        [424, 23712, 9496, True, True],
        [656, 1232, 8648, True, True],
        [16120, 240, 167584, True, False],
        [79008, 72, 6464, True, False],
        [1920, 1848, 75320, False, True],
        [912, 8328, 33576, True, False],
        [216, 144544, 2728, True, True],
        [272, 2336, 920, True, True],
        [12368, 232, 27184, False, False],
        [12464, 336, 712, False, True],
        [424, 2008, 9432, True, True],
        [240, 2744, 9624, False, True],
        [200, 2896, 6400, True, True],
        [1208, 1224, 10576, False, False],
        [4584, 2120, 2760, True, True],
        [1000, 204, 2048, False, True],
        [336, 3024, 13200, False, False],
        [128, 221184, 256, True, True],
        [3976, 1408, 22808, True, True],
        [88, 23512, 5824, True, True],
        [4776, 1216, 10696, False, True],
        [16, 3664, 1928, True, True],
        [144, 6424, 2080, False, True],
        [2920, 1072, 10648, False, False],
        [1648, 912, 5976, True, False],
        [168, 4784, 13440, True, True],
        [264, 12336, 1864, False, False],
        [1944, 1656, 2136, True, True],
        [400, 4280, 143912, True, True],
        [640, 5280, 1528, True, True],
        [264, 2672, 824, True, True],
        [240, 9776, 56472, True, False],
        [376, 1736, 9624, False, False],
        [240, 4824, 1064, True, False],
        [1256, 4392, 1312, False, False],
        [1360, 2200, 20248, True, True],
        [21680, 120, 24168, False, True],
        [584, 1144, 16976, False, False],
        [1184, 2032, 21600, True, False],
        [176, 4824, 5464, True, True],
        [2160, 1016, 6120, True, False],
        [544, 1664, 67952, False, True],
        [368, 5416, 23784, False, False],
        [9456, 192, 6360, False, False],
        [2456, 8096, 840, True, False],
        [3008, 2040, 800, False, False],
        [8312, 1160, 1320, True, False],
        [2048, 1152, 33656, False, False],
        [928, 720, 2360, True, True],
        [1176, 3296, 12296, False, True],
        [488, 4016, 22112, True, False],
        [288, 16384, 96, True, True],
        [1024, 480, 6912, True, True],
        [616, 2048, 10224, True, True],
        [1968, 296, 376, True, False],
        [200, 10608, 20232, True, False],
        [1792, 392, 10984, True, False],
        [1448, 840, 6920, True, False],
        [1048, 1400, 12376, True, False],
        [408, 2384, 1496, False, True],
        [504, 1328, 7208, False, False],
        [784, 968, 137104, False, True],
        [256, 1048576, 256, False, True],
        [80, 16096, 43064, True, False],
        [1976, 592, 2864, True, True],
        [392, 1544, 3176, True, True],
        [720, 2264, 1744, True, True],
        [7096, 2696, 11304, False, True],
        [1024, 1024, 4096, True, False],
        [368, 1304, 3088, True, False],
        [760, 1088, 26440, False, False],
        [8464, 888, 1032, False, True],
        [368, 4360, 5640, False, False],
        [488, 6744, 100504, False, False],
        [12488, 352, 23712, True, False],
        [280, 3424, 1336, True, True],
        [768, 8488, 8808, False, True],
        [3048, 376, 10072, True, False],
        [1312, 2032, 5400, True, True],
        [744, 12648, 17736, False, True],
        [2800, 304, 50416, True, False],
        [8784, 1816, 1280, True, False],
        [1648, 520, 1640, True, True],
        [1176, 736, 1064, True, False],
        [3832, 1680, 16840, False, False],
        [760, 5944, 15704, True, True],
        [904, 7032, 1912, True, False],
        [1288, 1296, 145816, True, False],
        [4936, 1144, 64792, True, True],
        [2376, 256, 4488, True, True],
        [1312, 5648, 808, True, False],
        [1128, 912, 1184, False, False],
        [3176, 760, 904, False, True],
        [1432, 1528, 40560, False, True],
        [896, 880, 12136, True, True],
        [36776, 184, 8696, True, True],
        [664, 3224, 17536, True, True],
        [176, 4320, 1320, False, True],
        [520, 1040, 5880, False, False],
        [256, 65536, 512, False, True],
        [14880, 208, 11608, False, True],
        [536, 3584, 5880, True, True],
        [320, 3560, 9008, True, False],
        [3008, 2368, 11448, False, True],
        [2672, 1016, 27496, False, False],
        [7312, 392, 1752, False, True],
        [712, 4776, 81816, True, False],
        [408, 55880, 2648, True, True],
        [5496, 400, 3248, False, False],
        [536, 13184, 2144, False, False],
        [12544, 1024, 512, True, False],
        [1536, 520, 1232, True, True],
        [120, 6648, 4520, False, True],
        [17680, 264, 14400, False, False],
        [1680, 1216, 14048, False, True],
        [2968, 1400, 51624, False, False],
        [520, 21152, 5176, False, False],
        [512, 1552, 11528, True, True],
        [4952, 864, 632, False, False],
        [6168, 5496, 3480, False, False],
        [1120, 5648, 704, False, True],
        [120, 9384, 3336, True, True],
        [1648, 1000, 8608, False, False],
        [536, 2608, 208, False, True],
        [2720, 1000, 17976, True, False],
        [504, 1688, 3592, True, True],
        [344, 35888, 11512, True, False],
        [1088, 880, 11504, True, False],
        [14736, 280, 3840, True, False],
        [1752, 656, 1816, True, False],
        [3480, 984, 28048, True, False],
        [15392, 288, 8616, False, True],
        [512, 2240, 256, True, True],
        [4840, 656, 18392, True, False],
        [6944, 120, 4808, False, True],
        [3184, 392, 4064, False, True],
        [3904, 400, 18648, True, True],
        [480, 1984, 38752, True, False],
        [120, 26480, 35176, True, False],
        [1472, 1448, 5512, True, True],
        [3976, 192, 13416, False, True],
        [7264, 824, 1664, False, False],
        [680, 18232, 12952, False, False],
        [1024, 2240, 1024, True, True],
        [1024, 2240, 1024, True, True],
        [392, 131784, 2816, False, False],
        [2536, 408, 1984, False, True],
        [496, 1488, 2736, False, True],
        [2408, 1368, 12488, False, False],
        [1688, 1736, 1672, True, True],
        [928, 4224, 29336, False, True],
        [864, 2368, 26824, False, False],
        [5048, 416, 14496, True, False],
        [1976, 1616, 568, False, False],
        [32224, 1040, 24400, True, True],
        [1512, 1416, 6368, False, True],
        [312, 6248, 9568, True, True],
        [2296, 9432, 4296, True, True],
        [376, 3352, 5136, False, False],
        [1320, 2920, 2744, False, False],
        [3792, 1336, 58904, True, True],
        [11800, 544, 7864, True, True],
        [1304, 2856, 3776, True, False],
        [16, 5560, 776, True, False],
        [1144, 1176, 21376, True, True],
        [264, 2880, 7384, True, False],
        [2704, 1472, 34856, False, True],
        [1064, 5872, 616, False, True],
        [2504, 2104, 2656, True, False],
        [192, 7840, 35672, True, False],
        [1024, 6912, 1024, True, True],
        [72, 10400, 1000, True, True],
        [664, 8448, 3360, True, False],
        [1264, 1784, 6360, True, True],
        [3376, 1328, 3192, False, False],
        [1496, 728, 3280, True, True],
        [240, 32408, 1968, True, False],
        [6168, 1840, 21656, False, False],
        [2472, 408, 4616, False, False],
        [3768, 4864, 5528, True, True],
        [5032, 728, 5304, True, True],
        [5888, 664, 6488, False, True],
        [1240, 9392, 4024, False, True],
        [824, 1336, 4784, False, False],
        [16616, 72, 9280, False, False],
        [1536, 11264, 512, False, True],
        [880, 1328, 560, False, False],
        [1040, 6000, 3312, True, True],
        [512, 2240, 1024, True, True],
        [2472, 7536, 1200, True, False],
        [20208, 3072, 600, True, True],
        [1024, 25088, 512, True, True],
        [4960, 992, 40792, True, True],
        [1048, 760, 59552, False, True],
        [2104, 1184, 27536, False, False],
        [20488, 1088, 4872, True, True],
        [480, 2296, 2312, False, True],
        [1024, 1024, 131072, True, False],
        [72, 5616, 4008, True, False],
        [176, 4816, 40064, False, True],
        [472, 2192, 3864, True, False],
        [13656, 896, 2856, True, True],
        [112, 30704, 904, True, True],
        [576, 2776, 20672, True, False],
        [6680, 384, 8944, True, False],
        [1024, 56000, 480, True, True],
        [1176, 7952, 7880, False, True],
        [592, 1288, 1032, True, False],
        [7512, 176, 6064, False, True],
        [712, 27376, 10568, True, True],
        [96, 9688, 1760, True, False],
        [392, 1968, 6536, True, False],
        [1528, 5304, 26616, True, False],
        [81496, 136, 6288, False, True],
        [13312, 672, 10856, True, True],
        [7648, 664, 17752, True, False],
        [552, 1248, 1672, False, True],
        [3344, 2784, 696, False, True],
        [5784, 1816, 24664, False, False],
        [1072, 2416, 1696, False, True],
        [1024, 1672, 3240, False, True],
        [816, 1232, 7416, True, True],
        [568, 2512, 1088, True, False],
        [1024, 1024, 221184, True, False],
        [1080, 3232, 6024, True, False],
        [448, 3200, 768, True, True],
        [256, 221184, 512, True, True],
        [1952, 2824, 5832, True, False],
        [1168, 3336, 1256, False, True],
        [256, 6912, 512, True, True],
        [112, 16840, 10728, False, False],
        [600, 2632, 82320, True, False],
        [4280, 1952, 1040, True, True],
        [2376, 1656, 1480, True, True],
        [12544, 512, 1024, True, True],
        [3712, 2096, 18744, False, False],
        [6752, 1184, 3408, False, True],
        [31984, 288, 7576, True, False],
        [1016, 11576, 4808, True, False],
        [480, 2240, 1024, True, True],
        [1176, 1552, 2624, False, False],
        [9448, 4680, 6552, False, True],
        [976, 1184, 1696, True, False],
        [232, 5488, 11408, False, True],
        [7416, 1368, 17760, False, False],
        [2752, 4656, 29704, False, False],
        [17608, 1648, 880, False, False],
        [1144, 20040, 4968, True, True],
        [784, 944, 3976, True, True],
        [1688, 3872, 3408, True, False],
        [688, 2544, 13904, False, False],
        [936, 14416, 2968, False, False],
        [3728, 1648, 968, False, True],
        [3472, 480, 48, True, True],
        [1336, 1344, 13488, True, True],
        [4376, 9544, 7120, True, False],
        [1024, 896, 4512, True, True],
        [1152, 1584, 52472, False, False],
        [4296, 13368, 1016, False, False],
        [1024, 25088, 512, False, True],
        [608, 4048, 1624, False, True],
        [2200, 872, 2320, True, False],
        [161344, 3512, 1272, False, False],
        [20208, 600, 3072, True, False],
        [136, 6232, 3624, True, True],
        [4384, 256, 3352, False, False],
        [2664, 936, 19632, False, True],
        [3248, 1376, 1904, False, False],
        [2440, 3728, 29048, False, False],
        [2512, 480, 2624, False, True],
        [3640, 5296, 22040, False, False],
        [2376, 18872, 1504, False, False],
        [3896, 256, 2368, False, True],
        [704, 2752, 50616, False, False],
        [1120, 1576, 16848, True, False],
        [1024, 1024, 6912, True, True],
        [15184, 2168, 2472, True, False],
        [568, 1304, 14896, False, False],
        [584, 3120, 29816, True, False],
        [216, 5416, 36880, False, True],
        [472, 11696, 27088, True, True],
        [784, 2864, 1360, False, False],
        [11192, 7720, 1736, False, False],
        [552, 1312, 6720, True, False],
        [96, 16384, 288, True, True],
        [7168, 2896, 3400, False, False],
        [1672, 1656, 40016, False, True],
        [1688, 6880, 8920, False, False],
        [2728, 1328, 4368, True, True],
        [920, 2144, 9720, False, True],
        [5264, 712, 14064, False, True],
        [2064, 3000, 10592, False, True],
        [2792, 600, 552, False, True],
        [2176, 808, 10160, True, True],
        [1024, 1024, 11200, True, False],
        [10816, 112, 3816, False, True],
        [1952, 744, 3384, False, True],
        [208, 13104, 1208, False, True],
        [2904, 1536, 1792, False, False],
        [1024, 40000, 512, False, True],
        [1472, 2792, 12128, True, False],
        [352, 2704, 6752, True, False],
        [928, 3040, 1928, True, False],
        [1376, 928, 24040, True, False],
        [752, 1608, 5128, False, True],
        [1912, 688, 800, False, False],
        [272, 9432, 9288, True, True],
        [576, 1560, 4640, False, True],
        [1072, 936, 1064, False, False],
        [400, 18536, 6592, True, True],
        [192, 92608, 5448, True, False],
        [7144, 14912, 624, True, False],
        [432, 146640, 6008, False, True],
        [1848, 672, 800, False, False],
        [2088, 4504, 6888, True, False],
        [8248, 1648, 1400, False, False],
        [28640, 1024, 640, True, True],
        [1048, 38824, 27320, False, True],
        [3296, 2608, 864, True, True],
        [504, 2048, 53056, False, True],
        [1608, 2328, 44944, True, True],
        [1592, 1072, 4784, True, False],
        [504, 2096, 21984, False, True],
        [808, 1248, 4688, True, False],
        [5480, 1680, 5800, True, False],
        [1736, 1344, 27528, True, False],
        [1240, 560, 1840, True, True],
        [17552, 5912, 1424, True, False],
        [384, 2576, 2712, False, True],
        [1504, 10136, 9120, True, False],
        [7552, 816, 4960, False, False],
        [776, 4376, 1104, True, False],
        [7264, 2032, 3096, False, False],
        [1040, 1576, 40640, False, True],
        [5432, 376, 6584, True, False],
        [4096, 3840, 1024, False, True],
        [1024, 1024, 2240, True, True],
        [1776, 8696, 31352, True, True],
        [9792, 312, 3944, False, True],
        [1552, 2784, 4624, False, True],
        [15264, 3696, 17128, False, False],
        [2488, 3648, 11600, True, True],
        [1024, 7240, 5856, False, False],
        [712, 2496, 39104, True, False],
        [752, 1576, 24752, False, False],
        [21440, 2456, 20520, False, False],
        [1904, 3192, 3432, False, True],
        [5344, 4168, 9416, False, False],
        [3592, 1136, 1280, False, False],
        [1576, 2792, 17616, False, False],
        [2192, 608, 14720, True, True],
        [1024, 8192, 1024, False, True],
        [368, 3032, 3952, True, True],
        [1136, 3912, 3848, False, False],
        [1024, 1000, 12544, False, True],
        [952, 984, 19184, True, True],
        [1824, 10776, 6816, True, True],
        [512, 6912, 1024, True, True],
        [3792, 1176, 5440, True, True],
        [16352, 720, 18512, True, False],
        [1024, 221184, 480, True, True],
        [1512, 17208, 4976, True, False],
        [1336, 18824, 7064, True, False],
        [5456, 368, 14480, False, True],
        [480, 6912, 1024, True, True],
        [768, 3528, 21968, True, True],
        [1024, 1000, 12544, False, True],
        [12392, 144, 3840, False, True],
        [1464, 896, 4512, True, False],
        [496, 1968, 8536, False, False],
        [5320, 688, 7520, False, True],
        [1056, 11384, 32392, True, False],
        [360, 23224, 45168, False, False],
        [3496, 176, 2512, True, True],
        [6696, 4688, 3352, True, False],
        [3000, 560, 2104, True, False],
        [4416, 968, 9440, False, False],
        [656, 1360, 2624, True, True],
        [1672, 696, 680, False, False],
        [20736, 2816, 1040, False, False],
        [7304, 208, 3656, False, False],
        [744, 3488, 5952, False, False],
        [2256, 1872, 23224, True, False],
        [1224, 3440, 1320, False, True],
        [176, 7224, 1032, False, False],
        [1496, 16104, 15888, False, True],
        [3840, 2144, 3848, False, False],
        [448, 3776, 2216, False, True],
        [1016, 1184, 22216, False, True],
        [1824, 4776, 15840, False, True],
        [1456, 5296, 33504, True, False],
        [587728, 296, 2440, True, True],
        [512, 40000, 1024, False, True],
        [1104, 16272, 4000, True, False],
        [12544, 2048, 1024, True, True],
        [1680, 5048, 3072, True, True],
        [608, 6352, 3352, False, True],
        [20208, 24576, 600, True, True],
        [1216, 22752, 2880, False, False],
        [4768, 17064, 4984, True, False],
        [5448, 624, 3120, True, True],
        [616, 12704, 14032, False, False],
        [16080, 2472, 6608, True, True],
        [2824, 536, 3024, True, True],
        [2808, 9064, 4688, True, False],
        [41736, 3848, 1784, True, True],
        [8064, 1208, 8304, False, True],
        [488, 2864, 8128, False, False],
        [42712, 4200, 2840, True, False],
        [13936, 464, 2560, False, True],
        [1120, 1424, 6288, False, True],
        [4040, 488, 3992, False, True],
        [3248, 1632, 8640, True, True],
        [464, 10352, 7488, True, True],
        [1288, 3552, 5072, True, True],
        [632, 2400, 3200, False, False],
        [3144, 1776, 13536, False, True],
        [1784, 2784, 6560, False, False],
        [2048, 204, 1000, True, True],
        [312, 22704, 21072, False, True],
        [23384, 1160, 3456, False, True],
        [984, 12624, 35680, False, True],
        [10528, 1024, 2680, False, False],
        [4096, 2560, 3072, True, True],
        [3000, 2688, 13952, True, False],
        [1160, 4048, 12960, False, True],
        [5896, 2352, 34480, False, True],
        [2752, 5328, 5200, True, False],
        [600, 3072, 20208, False, True],
        [1520, 1376, 2160, False, True],
        [2472, 31288, 3008, True, True],
        [1968, 3488, 9312, True, True],
        [7432, 224, 1648, True, True],
        [2712, 4512, 3296, False, False],
        [1520, 1672, 9752, False, True],
        [208, 5664, 2688, False, True],
        [1568, 2112, 11984, True, False],
        [936, 1616, 3512, False, True],
        [3392, 4608, 20080, False, True],
        [256, 5368, 78928, True, True],
        [512, 221184, 1024, True, True],
        [208, 3728, 3544, True, False],
        [11368, 27992, 2104, False, False],
        [512, 56000, 1024, True, True],
        [640, 3280, 15520, False, True],
        [1024, 4096, 1024, True, True],
        [1096, 99328, 10784, False, True],
        [15568, 1408, 6696, False, False],
        [1024, 4096, 1024, True, True],
        [76144, 2360, 2208, True, False],
        [768, 98304, 768, True, True],
        [2904, 3008, 57424, False, True],
        [12544, 8192, 12544, True, True],
        [1024, 4096, 1024, False, True],
        [256, 15312, 11328, False, True],
        [32320, 2560, 1024, True, True],
        [6352, 1384, 7480, False, True],
        [4832, 7304, 14032, False, True],
        [20096, 3320, 8152, True, False],
        [16040, 10960, 1392, True, False],
        [7840, 3144, 13840, True, True],
        [3072, 49152, 768, True, True],
        [121408, 2304, 1608, True, True],
        [1024, 56000, 1024, True, True],
        [568, 63688, 21616, False, True],
        [3072, 1728, 1024, False, True],
        [1544, 4608, 37472, False, False],
        [4896, 8432, 12704, True, False],
        [56224, 1640, 6984, True, True],
        [1768, 2016, 5568, False, True],
        [12544, 1024, 2048, True, False],
        [48688, 3136, 6816, False, False],
        [1024, 28416, 32320, False, True],
        [640, 52968, 13280, False, False],
        [384, 9064, 3968, False, True],
        [128128, 1976, 1640, False, True],
        [1728, 6584, 11584, False, True],
        [1024, 3072, 28672, True, False],
        [8752, 23504, 1536, False, True],
        [1024, 3840, 4096, False, True],
        [4728, 11856, 18672, True, False],
        [7800, 8168, 2784, False, True],
        [1024, 131072, 1024, True, True],
        [3072, 98304, 768, True, True],
        [12360, 3968, 6816, False, True],
        [560, 1656, 3504, True, True],
        [8192, 9352, 11072, True, False],
        [1024, 3840, 1024, False, True],
        [368, 9488, 55296, False, True],
        [1024, 3072, 24576, True, False],
        [320, 5032, 1184, True, True],
        [896, 8976, 4352, True, True],
        [768, 49152, 3072, True, True],
        [1024, 131072, 1024, False, True],
        [1024, 3712, 32320, False, True],
        [1024, 221184, 1024, True, True],
        [2776, 640, 3712, False, False],
        [1024, 28672, 4096, True, True],
        [14728, 1360, 5344, False, False],
        [512, 21808, 4240, False, True],
        [384, 5216, 1528, False, True],
        [2640, 8056, 12480, True, True],
        [26448, 4224, 9680, False, True],
        [1024, 24576, 4096, False, True],
        [32320, 28416, 1024, False, True],
        [1024, 24576, 4096, True, True],
        [4224, 38784, 4568, True, False],
        [1024, 28672, 4096, False, True],
        [1024, 221184, 1024, False, True],
        [4096, 1024, 24576, True, False],
        [4096, 1024, 28672, True, False],
        [480, 4032, 7496, True, False],
        [12544, 1024, 12544, True, False],
        [5720, 11520, 9952, True, False],
        [1024, 4096, 24576, True, False],
        [1792, 10472, 14272, True, True],
        [3808, 13568, 15488, True, False],
        [4096, 24576, 1024, False, True],
        [1024, 32320, 28416, True, False],
        [1024, 2048, 12544, False, True],
        [1024, 24000, 12544, True, True],
        [624, 2240, 3080, True, False],
        [4096, 28672, 1024, False, True],
        [512, 25088, 1024, False, True],
    ]
    return full_test if full else fast_test


def test_matmul(epilogue, is_hopper, persistent):
    for case in problem_list(True):
        M, N, K, TRANS_A, TRANS_B = case
        print(M, N, K, TRANS_A, TRANS_B)
        if (TRANS_A):
            a = torch.randn((K, M), device='cuda', dtype=torch.float16).T
            a_order = [0, 1]
        else:
            a = torch.randn((M, K), device='cuda', dtype=torch.float16)
            a_order = [1, 0]

        if (TRANS_B):
            b = torch.randn((N, K), device='cuda', dtype=torch.float16).T
            b_order = [0, 1]
        else:
            b = torch.randn((K, N), device='cuda', dtype=torch.float16)
            b_order = [1, 0]

        bias = None
        if epilogue in ['Bias', 'ReLuBias']:
            bias = torch.randn((M, 1), device='cuda', dtype=torch.float16)

        z = torch.randn((M, N), device='cuda', dtype=torch.float16)

        # torch result
        a_f32 = a.to(torch.float32)
        b_f32 = b.to(torch.float32)
        dot = torch.matmul(a_f32, b_f32)

        def process_epilogue(d, b, z, epilogue):
            if epilogue == 'add-matrix':
                ref = d + z
            elif epilogue == 'Bias':
                ref = d + b
            elif epilogue == 'ReLu':
                ref = torch.nn.functional.relu(d)
            elif epilogue == 'ReLuBias':
                ref = d + b
                ref = torch.nn.functional.relu(ref)
            else:
                ref = d
            return ref
        golden = process_epilogue(dot, bias, z, epilogue)

        num_SMs = torch.cuda.get_device_properties('cuda').multi_processor_count
        z = matmul(a, b, a_order, b_order, bias, z, is_hopper=is_hopper, epilogue=epilogue, persistent=persistent, num_sm=num_SMs)

        golden = torch.nn.functional.normalize(golden)
        z = torch.nn.functional.normalize(z)
        torch.set_printoptions(profile="full")
        assert_close(z, golden, rtol=1e-2, atol=1e-3, check_dtype=False)


@triton.testing.perf_report(
    triton.testing.Benchmark(
        # argument names to use as an x-axis for the plot
        x_names=['M', 'N', 'K', 'TRANS_A', 'TRANS_B'],
        x_vals=problem_list(False),  # different possible values for `x_name`
        line_arg='provider',
        # argument name whose value corresponds to a different line in the plot
        # possible values for `line_arg``
        line_vals=['cublas', 'triton'],
        # label name for the lines
        line_names=["cuBLAS", "Triton"],
        # line styles
        styles=[('green', '-'), ('green', '--'),
                ('blue', '-'), ('blue', '--')],
        ylabel="TFLOPS",  # label name for the y-axis
        plot_name="matmul-performance",
        # name for the plot. Used also as a file name for saving the plot.
        args={},
    )
)
def benchmark(M, N, K, TRANS_A, TRANS_B, provider):
    if (TRANS_A):
        a = torch.randn((K, M), device='cuda', dtype=torch.float16).T
        a_order = [0, 1]
    else:
        a = torch.randn((M, K), device='cuda', dtype=torch.float16)
        a_order = [1, 0]

    if (TRANS_B):
        b = torch.randn((N, K), device='cuda', dtype=torch.float16).T
        b_order = [0, 1]
    else:
        b = torch.randn((K, N), device='cuda', dtype=torch.float16)
        b_order = [1, 0]

    epilogue = 'none'
    # epilogue = 'add-matrix'
    # epilogue = 'Bias'
    # epilogue = 'ReLu'
    # epilogue = 'ReLuBias'
    is_hopper = True
    persistent = True
    # persistent = False
    num_SMs = torch.cuda.get_device_properties('cuda').multi_processor_count
    bias = None
    if epilogue in ['Bias', 'ReLuBias']:
        bias = torch.randn((M, 1), device='cuda', dtype=torch.float16)

    z = torch.randn((M, N), device='cuda', dtype=torch.float16)

    quantiles = [0.5, 0.2, 0.8]
    if provider == 'cublas':
        ms, min_ms, max_ms = triton.testing.do_bench(
            lambda: torch.matmul(a, b), rep=100, quantiles=quantiles, fast_flush=False)
    if provider == 'triton':
        ms, min_ms, max_ms = triton.testing.do_bench(
            lambda: matmul(a, b, a_order, b_order, bias=bias, z=z, is_hopper=is_hopper, epilogue=epilogue, persistent=persistent, num_sm=num_SMs), rep=100, quantiles=quantiles, fast_flush=False)

    def perf(ms):
        return 2 * M * N * K * 1e-12 / (ms * 1e-3)
    return perf(ms), perf(max_ms), perf(min_ms)


# test_matmul('none', is_hopper=True, persistent=True)
# test_matmul('add-matrix', is_hopper=True, persistent=False)
# test_matmul('Bias', is_hopper=True, persistent=False)
# test_matmul('ReLu', is_hopper=True, persistent=False)
# test_matmul('ReLuBias', is_hopper=True, persistent=False)
benchmark.run(show_plots=False, print_data=True)
