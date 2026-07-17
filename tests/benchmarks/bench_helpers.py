import math


def light(x):
    return x * 2 + 1


def heavy(x):
    acc = 0.0
    for k in range(400):
        acc += math.sin(x * 0.001 + k) * math.cos(k * 0.5)
    return acc


def medium(x):
    acc = 0.0
    for k in range(40):
        acc += math.sqrt(abs(x) + k)
    return acc


def combine(parent, weight):
    return parent * 0.5 + weight
