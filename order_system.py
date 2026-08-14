class OrderError(Exception):
    pass


class InvalidItemError(OrderError):
    pass


class Item:
    def __init__(self, name, price, quantity):
        self.name = name
        self.price = price
        self.quantity = quantity

    def total(self):
        return self.price * self.quantity

    def __repr__(self):
        return f"Item({self.name}, {self.total()})"


class Discountable:
    def apply_discount(self, percent):
        return self.total() * (1 - percent / 100)


class DiscountedItem(Item, Discountable):
    def __init__(self, name, price, quantity, discount):
        super().__init__(name, price, quantity)
        self.discount = discount

    def total(self):
        base = super().total()
        return self.apply_discount(self.discount) if self.discount else base


class Order:
    def __init__(self, order_id):
        self.order_id = order_id
        self.items = []

    def add_item(self, item):
        if item.quantity <= 0:
            raise InvalidItemError(f"Bad quantity for {item.name}")
        self.items.append(item)

    def subtotal(self):
        return sum(item.total() for item in self.items)

    def taxed_total(self, tax_rate=0.08):
        return self.subtotal() * (1 + tax_rate)

    def item_names(self):
        return [item.name for item in self.items]

    def cheapest_item(self):
        return min(self.items, key=lambda item: item.total())

    def summary(self):
        try:
            return f"Order {self.order_id}: {self.taxed_total():.2f}"
        except InvalidItemError as e:
            return f"Order {self.order_id} failed: {e}"


class OrderBatchProcessor:
    def __init__(self):
        self.orders = []

    def register(self, order):
        self.orders.append(order)

    def process_all(self):
        for order in self.orders:
            yield order.summary()

    @staticmethod
    def build_default_order(order_id):
        order = Order(order_id)
        order.add_item(Item("Widget", 10.0, 2))
        order.add_item(DiscountedItem("Gadget", 50.0, 1, 20))
        return order

    @property
    def order_count(self):
        return len(self.orders)


class LoggingBatchProcessor(OrderBatchProcessor):
    def register(self, order):
        print(f"Registering order {order.order_id}")
        super().register(order)


def run_batch():
    processor = LoggingBatchProcessor()
    order = OrderBatchProcessor.build_default_order(1)
    processor.register(order)
    for summary in processor.process_all():
        print(summary)
    print(f"Total orders: {processor.order_count}")
