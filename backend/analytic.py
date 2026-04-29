# analytics.py

def get_dashboard_stats(supabase, business_id: int):

    # Orders
    orders_res = supabase.table("orders") \
        .select("*") \
        .eq("business_id", business_id) \
        .execute()

    orders = orders_res.data or []

    total_orders = len(orders)
    total_revenue = sum(float(o.get("total", 0)) for o in orders)

    # Customers (unique phones)
    customers = set(o.get("customer_phone") for o in orders if o.get("customer_phone"))
    total_customers = len(customers)

    # Orders per day
    orders_per_day = {}
    for o in orders:
        date = str(o.get("created_at", ""))[:10]
        if date:
            orders_per_day[date] = orders_per_day.get(date, 0) + 1

    # Top products
    product_sales = {}
    for o in orders:
        items = o.get("items", [])
        for item in items:
            name = item.get("name")
            qty = item.get("quantity", 0)
            product_sales[name] = product_sales.get(name, 0) + qty

    top_products = sorted(product_sales.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "total_orders": total_orders,
        "total_revenue": total_revenue,
        "total_customers": total_customers,
        "orders_per_day": orders_per_day,
        "top_products": top_products
    }