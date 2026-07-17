import array

import bench_helpers as bh


def w_light(xs_slice):
    return [x * 2 + 1 for x in xs_slice]


def w_medium(xs_slice):
    return [bh.medium(x) for x in xs_slice]


def w_heavy(xs_slice):
    return [bh.heavy(x) for x in xs_slice]


def w_light_sum(xs_slice):
    total = 0
    for x in xs_slice:
        total += x
    return total


def w_heavy_sum(xs_slice):
    total = 0.0
    for x in xs_slice:
        total += bh.heavy(x)
    return total


def w_buffer(xs_arr):
    return array.array("d", [x * 2.0 + 1.0 for x in xs_arr])


def w_nested(rows_slice):
    out = [0.0] * len(rows_slice)
    for k, row in enumerate(rows_slice):
        s = 0.0
        for v in row:
            s += bh.medium(v)
        out[k] = s
    return out


def w_dag_level(parents, weights):
    return [bh.combine(p, w) for p, w in zip(parents, weights)]
