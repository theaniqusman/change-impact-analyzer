from .base import Widget  # relative import - "the module next to me in this package"


def build_widget(name):
    return Widget(name)


operations = {"build": build_widget}


def run(name):
    handler = operations["build"]
    return handler(name)  # INDIRECT call - the name "handler" isn't the function itself,
    # it's a variable that HAPPENS to hold one. Can the LSP still trace this?
