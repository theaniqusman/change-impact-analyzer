from sample_shapes import Rectangle


def compute_perimeter(width, height):
    return 2 * (width + height)


class Square(Rectangle):
    def __init__(self, side):
        super().__init__(side, side)

    def perimeter(self):
        return compute_perimeter(self.width, self.height)

    @staticmethod
    def unit_square():
        return Square(1)


def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n - 1)  # recursive: calls itself
