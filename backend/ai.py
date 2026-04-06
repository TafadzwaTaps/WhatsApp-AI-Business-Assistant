def generate_reply(message: str):
    message = message.lower()

    if "menu" in message:
        return "Here is our menu: \n1. Rice - $2\n2. Chicken - $5"

    elif "order" in message:
        return "Please type: order <product> <quantity>"

    return "Hello 👋, type 'menu' to see available products."