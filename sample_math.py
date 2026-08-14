from sample_utils import add, subtract


def multiply(a, b):
    total = 0
    for _ in range(b):
        total = add(total, a)
    return total


def difference(a, b):
    return subtract(a, b)
