import numpy as np
import taichi as ti

real = ti.f32
ti.init(default_fp=real,
        arch=ti.cuda,
        async_mode=True,
        async_opt_listgen=True,
        async_opt_dse=True,
        async_opt_activation_demotion=True,
        async_opt_fusion=True,
        kernel_profiler=False
        #, async_opt_intermediate_file="mgpcg"
        )

# grid parameters
N = 128
N_gui = 512  # gui resolution

n_mg_levels = 5
pre_and_post_smoothing = 2
bottom_smoothing = 150

use_multigrid = True

N_ext = N // 2  # number of ext cells set so that that total grid size is still power of 2
N_tot = 2 * N

# setup sparse simulation data arrays
r = [ti.field(dtype=real) for _ in range(n_mg_levels)]  # residual
z = [ti.field(dtype=real) for _ in range(n_mg_levels)]  # M^-1 r
x = ti.field(dtype=real)  # solution
p = ti.field(dtype=real)  # conjugate gradient
Ap = ti.field(dtype=real)  # matrix-vector product
alpha = ti.field(dtype=real)  # step size
beta = ti.field(dtype=real)  # step size
sum = ti.field(dtype=real)  # storage for reductions
pixels = ti.field(dtype=real, shape=(N_gui, N_gui))  # image buffer
rTr = ti.field(dtype=real, shape=())
old_zTr = ti.field(dtype=real, shape=())
new_zTr = ti.field(dtype=real, shape=())
pAp = ti.field(dtype=real, shape=())

grid = ti.root.pointer(ti.ijk, [N_tot // 4]).dense(ti.ijk, 4).place(x, p, Ap)

for l in range(n_mg_levels):
    grid = ti.root.pointer(ti.ijk,
                           [N_tot // (4 * 2**l)]).dense(ti.ijk,
                                                        4).place(r[l], z[l])

ti.root.place(alpha, beta, sum)


@ti.kernel
def init():
    for i, j, k in ti.ndrange((N_ext, N_tot - N_ext), (N_ext, N_tot - N_ext),
                              (N_ext, N_tot - N_ext)):
        xl = (i - N_ext) * 2.0 / N_tot
        yl = (j - N_ext) * 2.0 / N_tot
        zl = (k - N_ext) * 2.0 / N_tot
        r[0][i, j, k] = ti.sin(2.0 * np.pi * xl) * ti.sin(
            2.0 * np.pi * yl) * ti.sin(2.0 * np.pi * zl)
        z[0][i, j, k] = 0.0
        Ap[i, j, k] = 0.0
        p[i, j, k] = 0.0
        x[i, j, k] = 0.0


@ti.kernel
def compute_Ap():
    for i, j, k in Ap:
        Ap[i,j,k] = 6.0 * p[i,j,k] - p[i+1,j,k] - p[i-1,j,k] \
                                   - p[i,j+1,k] - p[i,j-1,k] \
                                   - p[i,j,k+1] - p[i,j,k-1]


@ti.kernel
def reduce(p: ti.template(), q: ti.template(), s: ti.template()):
    s[None] = 0
    for I in ti.grouped(p):
        s[None] += p[I] * q[I]


@ti.kernel
def update_x():
    for I in ti.grouped(p):
        x[I] += alpha[None] * p[I]


@ti.kernel
def update_r():
    for I in ti.grouped(p):
        r[0][I] -= alpha[None] * Ap[I]


@ti.kernel
def update_p():
    for I in ti.grouped(p):
        p[I] = z[0][I] + beta[None] * p[I]


@ti.kernel
def restrict(l: ti.template()):
    for i, j, k in r[l]:
        res = r[l][i, j, k] - (6.0 * z[l][i, j, k] - z[l][i + 1, j, k] -
                               z[l][i - 1, j, k] - z[l][i, j + 1, k] -
                               z[l][i, j - 1, k] - z[l][i, j, k + 1] -
                               z[l][i, j, k - 1])
        r[l + 1][i // 2, j // 2, k // 2] += res * 0.5


@ti.kernel
def prolongate(l: ti.template()):
    for I in ti.grouped(z[l]):
        z[l][I] = z[l + 1][I // 2]


@ti.kernel
def smooth(l: ti.template(), phase: ti.template()):
    # phase = red/black Gauss-Seidel phase
    for i, j, k in r[l]:
        if (i + j + k) & 1 == phase:
            z[l][i,j,k] = (r[l][i,j,k] + z[l][i+1,j,k] + z[l][i-1,j,k] \
                                       + z[l][i,j+1,k] + z[l][i,j-1,k] \
                                       + z[l][i,j,k+1] + z[l][i,j,k-1])/6.0


def apply_preconditioner():
    z[0].fill(0)
    for l in range(n_mg_levels - 1):
        for i in range(pre_and_post_smoothing << l):
            smooth(l, 0)
            smooth(l, 1)
        z[l + 1].fill(0)
        r[l + 1].fill(0)
        restrict(l)

    for i in range(bottom_smoothing):
        smooth(n_mg_levels - 1, 0)
        smooth(n_mg_levels - 1, 1)

    for l in reversed(range(n_mg_levels - 1)):
        prolongate(l)
        for i in range(pre_and_post_smoothing << l):
            smooth(l, 1)
            smooth(l, 0)


@ti.kernel
def paint():
    kk = N_tot * 3 // 8
    for i, j in pixels:
        ii = int(i * N / N_gui) + N_ext
        jj = int(j * N / N_gui) + N_ext
        pixels[i, j] = x[ii, jj, kk] / N_tot


gui = ti.GUI("mgpcg", res=(N_gui, N_gui))

init()

sum[None] = 0.0
reduce(r[0], r[0], rTr)
initial_rTr = rTr[None]

# r = b - Ax = b    since x = 0
# p = r = r + 0 p
if use_multigrid:
    apply_preconditioner()
else:
    z[0].copy_from(r[0])

update_p()

sum[None] = 0.0
reduce(z[0], r[0], old_zTr)
print('old_zTr', old_zTr)


@ti.kernel
def print_rTr():
    print('rTr', rTr[None])


@ti.kernel
def update_alpha():
    alpha[None] = old_zTr[None] / pAp[None]


@ti.kernel
def update_beta():
    beta[None] = new_zTr[None] / old_zTr[None]
    old_zTr[None] = new_zTr[None]


# CG
for i in range(10):
    # alpha = rTr / pTAp
    compute_Ap()
    reduce(p, Ap, pAp)
    update_alpha()

    # ti.sync()
    # x = x + alpha p
    update_x()

    # r = r - alpha Ap
    update_r()

    # check for convergence
    reduce(r[0], r[0], rTr)
    print_rTr()

    # z = M^-1 r
    if use_multigrid:
        apply_preconditioner()
    else:
        z[0].copy_from(r[0])

    # beta = new_rTr / old_rTr
    reduce(z[0], r[0], new_zTr)
    update_beta()

    # p = z + beta p
    update_p()

paint()
gui.set_image(pixels)
gui.show()

ti.kernel_profiler_print()
ti.core.print_stat()