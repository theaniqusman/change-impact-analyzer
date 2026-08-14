class Loggable:
    def log(self, message):
        print(f"[LOG] {message}")


class Serializable:
    def serialize(self):
        return str(self.__dict__)


class Widget(Loggable, Serializable):  # multiple inheritance - two parents at once
    def __init__(self, name):
        self.name = name

    def show(self):
        self.log(f"Showing {self.name}")       # inherited from Loggable
        return self.serialize()                # inherited from Serializable
