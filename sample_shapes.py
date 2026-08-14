class Shape:
    def area(self):
        raise NotImplementedError

    def describe(self):
        return f"This shape has area {self.area()}"


class Rectangle(Shape):
    def __init__(self, width, height):
        self.width = width
        self.height = height

    def area(self):
        return self.width * self.height
