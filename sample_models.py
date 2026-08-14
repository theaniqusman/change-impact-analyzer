from sample_math import multiply, difference


class Calculator:
    def compute(self, a, b):
        return multiply(a, b)

    def compute_twice(self, a, b):
        return self.compute(a, b) + self.compute(a, b)


class ScientificCalculator(Calculator):
    def compute_difference(self, a, b):
        return difference(a, b)
