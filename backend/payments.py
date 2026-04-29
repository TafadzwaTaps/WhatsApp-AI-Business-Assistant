# payments.py

def confirm_payment(supabase, reference: str, amount: float):
    """
    reference format: ORDER-12
    """

    if not reference.startswith("ORDER-"):
        return {"error": "Invalid reference"}

    try:
        order_id = int(reference.split("-")[1])
    except:
        return {"error": "Invalid reference format"}

    # Get order
    res = supabase.table("orders").select("*").eq("id", order_id).execute()

    if not res.data:
        return {"error": "Order not found"}

    order = res.data[0]

    # Validate amount
    if float(order["total"]) != float(amount):
        return {"error": "Amount mismatch"}

    # Update order
    supabase.table("orders").update({
        "status": "paid",
        "payment_status": "paid",
        "payment_reference": reference
    }).eq("id", order_id).execute()

    return {
        "message": "Payment confirmed",
        "order_id": order_id
    }