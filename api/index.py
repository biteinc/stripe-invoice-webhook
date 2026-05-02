import json
import os
from http.server import BaseHTTPRequestHandler

import stripe

stripe.api_key = os.environ["SURCHARGE_STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["SURCHARGE_STRIPE_WEBHOOK_SECRET"]
SURCHARGE_PRODUCT_ID = os.environ.get("SURCHARGE_PRODUCT_ID", "prod_TwsauvTg8JPMTs")
SURCHARGE_RATE = 0.03


def sget(obj, key, default=None):
    """Safe get for both dicts and StripeObjects."""
    try:
        val = obj[key]
        return val if val is not None else default
    except (KeyError, IndexError, TypeError):
        return default


def to_plain(obj):
    """Recursively convert StripeObject to plain dict/list."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_plain(i) for i in obj]
    return obj


def get_payment_method_type(subscription):
    """
    Determines payment method type by looking at the most recent successfully
    paid invoice's payment intent. This is the only reliable signal that a
    subscription is genuinely on autopay -- a card on file at the customer or
    subscription level does NOT mean autopay. Only a paid invoice proves it.
    """
    try:
        invoices = stripe.Invoice.list(
            subscription=subscription["id"],
            status="paid",
            limit=1,
            expand=["data.payment_intent"],
        )
        if invoices["data"]:
            invoice = invoices["data"][0]
            pi = sget(invoice, "payment_intent")
            if pi and isinstance(pi, dict):
                pm_id = sget(pi, "payment_method")
                if pm_id:
                    if isinstance(pm_id, dict):
                        return pm_id.get("type")
                    pm = stripe.PaymentMethod.retrieve(pm_id)
                    return pm["type"]
    except Exception:
        pass

    return None


def find_surcharge_item(subscription):
    for item in subscription["items"]["data"]:
        price_field = item["price"]
        price = price_field if not isinstance(price_field, str) else stripe.Price.retrieve(price_field)
        if price["product"] == SURCHARGE_PRODUCT_ID:
            return item
    return None


def calculate_surcharge_cents(subscription):
    total = 0
    for item in subscription["items"]["data"]:
        price_field = item["price"]
        price = price_field if not isinstance(price_field, str) else stripe.Price.retrieve(price_field)
        if price["product"] == SURCHARGE_PRODUCT_ID:
            continue
        unit_amount = sget(price, "unit_amount") or 0
        quantity = sget(item, "quantity") or 1
        total += unit_amount * quantity
    return round(total * SURCHARGE_RATE)


def get_or_create_surcharge_price(amount_cents, interval):
    existing = stripe.Price.list(product=SURCHARGE_PRODUCT_ID, active=True, limit=100)
    for p in existing["data"]:
        recurring = sget(p, "recurring") or {}
        if (
            p["unit_amount"] == amount_cents
            and sget(recurring, "interval") == interval
            and p["currency"] == "usd"
        ):
            return p["id"]
    new_price = stripe.Price.create(
        product=SURCHARGE_PRODUCT_ID,
        unit_amount=amount_cents,
        currency="usd",
        recurring={"interval": interval},
        nickname=f"CC Surcharge ${amount_cents / 100:.2f}/{interval}",
        tax_behavior="exclusive",
    )
    return new_price["id"]


def remove_surcharge_from_subscription(sub):
    surcharge_item = find_surcharge_item(sub)
    if not surcharge_item:
        print(f"[{sub['id']}] No surcharge item — nothing to remove.")
        return
    stripe.SubscriptionItem.delete(surcharge_item["id"], proration_behavior="none")
    print(f"[{sub['id']}] Surcharge REMOVED")


def add_surcharge_to_subscription(sub):
    if find_surcharge_item(sub):
        print(f"[{sub['id']}] Surcharge already present — skipping.")
        return
    surcharge_cents = calculate_surcharge_cents(sub)
    if surcharge_cents <= 0:
        print(f"[{sub['id']}] Surcharge is 0 — skipping.")
        return
    primary_item = next(
        (i for i in sub["items"]["data"] if i["price"]["product"] != SURCHARGE_PRODUCT_ID),
        None
    )
    if primary_item:
        recurring = sget(primary_item["price"], "recurring") or {}
        interval = sget(recurring, "interval") or "month"
    else:
        interval = "month"
    price_id = get_or_create_surcharge_price(surcharge_cents, interval)
    stripe.SubscriptionItem.create(
        subscription=sub["id"],
        price=price_id,
        quantity=1,
        proration_behavior="none",
    )
    print(f"[{sub['id']}] Surcharge ADDED — ${surcharge_cents / 100:.2f}/{interval}")


def recalculate_surcharge(sub):
    remove_surcharge_from_subscription(sub)
    sub = stripe.Subscription.retrieve(sub["id"], expand=["items.data.price"])
    add_surcharge_to_subscription(sub)


def handle_subscription_updated(event):
    new_sub = event["data"]["object"]
    try:
        raw_previous = event["data"]["previous_attributes"]
        previous = to_plain(raw_previous) if raw_previous else {}
    except (KeyError, AttributeError):
        previous = {}

    print(f"previous keys: {list(previous.keys())}")

    sub = stripe.Subscription.retrieve(new_sub["id"], expand=["items.data.price"])
    pm_type = get_payment_method_type(sub)
    pm_changed = "default_payment_method" in previous

    items_changed = False
    if "items" in previous:
        items_data = previous.get("items", {}).get("data", [])
        print(f"previous items: {len(items_data)}")
        for item in items_data:
            try:
                product = item["price"]["product"]
            except Exception:
                product = None
            print(f"  item product: {product}")
            if product != SURCHARGE_PRODUCT_ID:
                items_changed = True
                break

    print(f"[{sub['id']}] Updated — pm_changed: {pm_changed}, items_changed: {items_changed}, pm_type: {pm_type}")

    if pm_changed:
        if pm_type == "card":
            add_surcharge_to_subscription(sub)
        else:
            remove_surcharge_from_subscription(sub)
    elif items_changed:
        if pm_type == "card":
            recalculate_surcharge(sub)
        else:
            print(f"[{sub['id']}] Items changed but not on card — no action needed")
    else:
        print(f"[{sub['id']}] No relevant changes — ignoring")


def handle_customer_updated(event):
    customer = event["data"]["object"]
    try:
        raw_previous = event["data"]["previous_attributes"]
        previous = to_plain(raw_previous) if raw_previous else {}
    except (KeyError, AttributeError):
        previous = {}

    if "invoice_settings" not in previous and "default_source" not in previous:
        print(f"[cus: {customer['id']}] No PM change — ignoring.")
        return
    subscriptions = stripe.Subscription.list(
        customer=customer["id"], status="active", limit=100,
        expand=["data.items.data.price"]
    )
    for sub in subscriptions["data"]:
        if sget(sub, "default_payment_method"):
            print(f"[{sub['id']}] Has own PM — skipping.")
            continue
        pm_type = get_payment_method_type(sub)
        print(f"[{sub['id']}] Customer PM changed → type: {pm_type}")
        if pm_type == "card":
            recalculate_surcharge(sub)
        else:
            remove_surcharge_from_subscription(sub)


def find_surcharge_invoice_item(invoice_id):
    """Check if a surcharge line item already exists on this draft invoice."""
    items = stripe.InvoiceItem.list(invoice=invoice_id, limit=100)
    for item in items["data"]:
        if sget(item, "metadata", {}).get("surcharge") == "true":
            return item
    return None


def handle_invoice_created(invoice):
    invoice_id = invoice["id"]
    sub_id = sget(invoice, "subscription")

    if not sub_id:
        print(f"Skipping {invoice_id} — not a subscription invoice")
        return

    # Only act on draft invoices — that's the only state invoice.created fires in
    if sget(invoice, "status") != "draft":
        print(f"Skipping {invoice_id} — status is {invoice['status']}, not draft")
        return

    sub = stripe.Subscription.retrieve(sub_id, expand=["items.data.price"])
    pm_type = get_payment_method_type(sub)
    print(f"Invoice {invoice_id} — pm_type: {pm_type}")

    if pm_type != "card":
        # If customer switched away from card, remove any existing surcharge invoice item
        existing = find_surcharge_invoice_item(invoice_id)
        if existing:
            stripe.InvoiceItem.delete(existing["id"])
            print(f"Invoice {invoice_id} — removed stale surcharge item (no longer card)")
        else:
            print(f"Skipping {invoice_id} — not a card")
        return

    # Guard: don't add a second surcharge if one already exists on this invoice
    if find_surcharge_invoice_item(invoice_id):
        print(f"Invoice {invoice_id} — surcharge item already present, skipping")
        return

    # Calculate from subscription base amount only — never from invoice total,
    # which would compound the surcharge on top of itself each cycle
    surcharge_amount = calculate_surcharge_cents(sub)
    if surcharge_amount <= 0:
        print(f"Skipping {invoice_id} — zero surcharge amount")
        return

    print(f"Adding ${surcharge_amount/100:.2f} surcharge to draft invoice {invoice_id}")
    try:
        stripe.InvoiceItem.create(
            customer=invoice["customer"],
            invoice=invoice_id,
            amount=surcharge_amount,
            currency=sget(invoice, "currency") or "usd",
            description="Credit Card Processing Fee (3%)",
            metadata={"surcharge": "true"}
        )
        print(f"✓ Surcharge added to invoice {invoice_id}")
    except stripe.error.StripeError as e:
        print(f"✗ Failed: {e}")


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)
        sig_header = self.headers.get("stripe-signature")

        try:
            event = stripe.Webhook.construct_event(raw_body, sig_header, WEBHOOK_SECRET)
        except stripe.error.SignatureVerificationError as e:
            print(f"Signature error: {e}")
            self._respond(400, {"error": str(e)})
            return

        print(f"Event: {event['type']} [{event['id']}]")

        try:
            if event["type"] == "customer.subscription.updated":
                handle_subscription_updated(event)
            elif event["type"] == "customer.updated":
                handle_customer_updated(event)
            elif event["type"] == "invoice.created":
                handle_invoice_created(event["data"]["object"])
            else:
                print(f"Unhandled: {event['type']}")
        except Exception as e:
            import traceback
            print(f"ERROR: {traceback.format_exc()}")

        self._respond(200, {"received": True})

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())
