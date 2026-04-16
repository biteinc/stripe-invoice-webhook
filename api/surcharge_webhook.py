import json
import os
from http.server import BaseHTTPRequestHandler
 
import stripe
 
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET_SURCHARGE"]
 
SURCHARGE_PRODUCT_ID = "prod_TwsauvTg8JPMTs"
SURCHARGE_RATE = 0.03
 
 
# ─── Helpers ──────────────────────────────────────────────────────────────────
 
def get_payment_method_type(subscription):
    """
    Returns the effective payment method type for a subscription.
    Checks subscription-level PM first, then falls back to customer-level.
    Returns 'card', 'us_bank_account', or None if unknown.
    """
    pm_id = subscription.get("default_payment_method")
    if pm_id:
        if isinstance(pm_id, dict):
            return pm_id["type"]
        pm = stripe.PaymentMethod.retrieve(pm_id)
        return pm["type"]
 
    customer = stripe.Customer.retrieve(
        subscription["customer"],
        expand=["invoice_settings.default_payment_method"]
    )
    invoice_pm = customer.get("invoice_settings", {}).get("default_payment_method")
    if invoice_pm:
        if isinstance(invoice_pm, str):
            pm = stripe.PaymentMethod.retrieve(invoice_pm)
            return pm["type"]
        return invoice_pm["type"]
 
    default_source = customer.get("default_source")
    if default_source:
        if isinstance(default_source, str):
            source = stripe.Customer.retrieve_source(customer["id"], default_source)
        else:
            source = default_source
        return "us_bank_account" if source["object"] == "bank_account" else source["object"]
 
    return None
 
 
def find_surcharge_item(subscription):
    """Returns existing surcharge subscription item or None."""
    for item in subscription["items"]["data"]:
        price = item["price"] if isinstance(item["price"], dict) else stripe.Price.retrieve(item["price"])
        if price["product"] == SURCHARGE_PRODUCT_ID:
            return item
    return None
 
 
def calculate_surcharge_cents(subscription):
    """Calculates the 3% surcharge amount in cents."""
    total = 0
    for item in subscription["items"]["data"]:
        price = item["price"] if isinstance(item["price"], dict) else stripe.Price.retrieve(item["price"])
        if price["product"] == SURCHARGE_PRODUCT_ID:
            continue
        total += (price.get("unit_amount") or 0) * (item.get("quantity") or 1)
    return round(total * SURCHARGE_RATE)
 
 
def get_or_create_surcharge_price(amount_cents, interval):
    """Returns an existing surcharge price for the given amount, or creates one."""
    existing = stripe.Price.list(product=SURCHARGE_PRODUCT_ID, active=True, limit=100)
    for p in existing["data"]:
        if (
            p["unit_amount"] == amount_cents
            and p.get("recurring", {}).get("interval") == interval
            and p["currency"] == "usd"
        ):
            return p["id"]
 
    new_price = stripe.Price.create(
        product=SURCHARGE_PRODUCT_ID,
        unit_amount=amount_cents,
        currency="usd",
        recurring={"interval": interval},
        nickname=f"CC Surcharge ${amount_cents / 100:.2f}/{interval}",
    )
    return new_price["id"]
 
 
def add_surcharge(subscription):
    """Adds a 3% surcharge item to a subscription if not already present."""
    if find_surcharge_item(subscription):
        print(f"[{subscription['id']}] Surcharge already present — skipping.")
        return
 
    surcharge_cents = calculate_surcharge_cents(subscription)
    if surcharge_cents <= 0:
        print(f"[{subscription['id']}] Surcharge amount is 0 — skipping.")
        return
 
    primary_item = next(
        (i for i in subscription["items"]["data"]
         if i["price"]["product"] != SURCHARGE_PRODUCT_ID),
        None
    )
    interval = primary_item["price"].get("recurring", {}).get("interval", "month") if primary_item else "month"
    price_id = get_or_create_surcharge_price(surcharge_cents, interval)
 
    stripe.SubscriptionItem.create(
        subscription=subscription["id"],
        price=price_id,
        quantity=1,
        proration_behavior="none",
    )
    print(f"[{subscription['id']}] Surcharge ADDED — ${surcharge_cents / 100:.2f}/{interval}")
 
 
def remove_surcharge(subscription):
    """Removes the surcharge item from a subscription if present."""
    surcharge_item = find_surcharge_item(subscription)
    if not surcharge_item:
        print(f"[{subscription['id']}] No surcharge item found — nothing to remove.")
        return
 
    stripe.SubscriptionItem.delete(
        surcharge_item["id"],
        proration_behavior="none",
    )
    print(f"[{subscription['id']}] Surcharge REMOVED")
 
 
# ─── Event Handlers ───────────────────────────────────────────────────────────
 
def handle_subscription_updated(event):
    """
    Fires when a subscription's own default_payment_method changes.
    Adds or removes surcharge based on the new payment method type.
    """
    new_sub = event["data"]["object"]
    previous = event["data"].get("previous_attributes", {})
 
    if "default_payment_method" not in previous:
        print(f"[{new_sub['id']}] subscription.updated — no PM change, ignoring.")
        return
 
    sub = stripe.Subscription.retrieve(
        new_sub["id"],
        expand=["items.data.price"]
    )
    pm_type = get_payment_method_type(sub)
    print(f"[{sub['id']}] PM changed → type: {pm_type}")
 
    if pm_type == "card":
        add_surcharge(sub)
    else:
        remove_surcharge(sub)
 
 
def handle_customer_updated(event):
    """
    Fires when a customer's default payment method changes at the customer level.
    Re-evaluates all active subscriptions for that customer that don't have
    their own subscription-level PM override.
    """
    customer = event["data"]["object"]
    previous = event["data"].get("previous_attributes", {})
 
    if "invoice_settings" not in previous and "default_source" not in previous:
        print(f"[cus: {customer['id']}] customer.updated — no PM change, ignoring.")
        return
 
    subscriptions = stripe.Subscription.list(
        customer=customer["id"],
        status="active",
        limit=100,
        expand=["data.items.data.price"]
    )
 
    for sub in subscriptions["data"]:
        # Skip subs with their own PM — handled by subscription.updated instead
        if sub.get("default_payment_method"):
            print(f"[{sub['id']}] Has own PM, skipping customer-level change.")
            continue
 
        pm_type = get_payment_method_type(sub)
        print(f"[{sub['id']}] Customer PM changed → effective type: {pm_type}")
 
        if pm_type == "card":
            add_surcharge(sub)
        else:
            remove_surcharge(sub)
 
 
# ─── Vercel Handler ───────────────────────────────────────────────────────────
 
class handler(BaseHTTPRequestHandler):
 
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)
        sig_header = self.headers.get("stripe-signature")
 
        try:
            event = stripe.Webhook.construct_event(
                raw_body, sig_header, WEBHOOK_SECRET
            )
        except stripe.error.SignatureVerificationError as e:
            self._respond(400, {"error": str(e)})
            return
 
        print(f"Received event: {event['type']} [{event['id']}]")
 
        try:
            if event["type"] == "customer.subscription.updated":
                handle_subscription_updated(event)
            elif event["type"] == "customer.updated":
                handle_customer_updated(event)
            else:
                print(f"Unhandled event type: {event['type']}")
        except Exception as e:
            print(f"X Failed: {e}")
 
        self._respond(200, {"received": True})
 
    def _respond(self, status: int, body: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())
