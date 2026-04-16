import os
import stripe

stripe.api_key = os.environ["SURCHARGE_STRIPE_SECRET_KEY"]

SURCHARGE_PRODUCT_ID = "prod_TwsauvTg8JPMTs"
SURCHARGE_RATE = 0.03
DRY_RUN = "--dry-run" in __import__("sys").argv

# Cache to avoid creating duplicate surcharge prices in the same run
price_cache = {}


def get_payment_method_type(subscription):
    """
    Returns the effective payment method type for a subscription.
    Checks subscription-level PM first, then falls back to customer-level.
    Returns 'card', 'us_bank_account', or None if unknown.
    """
    # 1. Subscription-level default payment method
    pm_id = subscription.get("default_payment_method")
    if pm_id:
        pm = stripe.PaymentMethod.retrieve(pm_id)
        return pm["type"]

    # 2. Customer-level default payment method
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

    # 3. Legacy default source
    default_source = customer.get("default_source")
    if default_source:
        if isinstance(default_source, str):
            source = stripe.Customer.retrieve_source(customer["id"], default_source)
        else:
            source = default_source
        return "us_bank_account" if source["object"] == "bank_account" else source["object"]

    return None


def find_surcharge_item(subscription):
    """
    Returns the existing surcharge subscription item if one exists, else None.
    """
    for item in subscription["items"]["data"]:
        price = item["price"] if isinstance(item["price"], dict) else stripe.Price.retrieve(item["price"])
        if price["product"] == SURCHARGE_PRODUCT_ID:
            return item
    return None


def calculate_surcharge_cents(subscription):
    """
    Calculates the 3% surcharge amount in cents based on the subscription total.
    """
    total = 0
    for item in subscription["items"]["data"]:
        price = item["price"] if isinstance(item["price"], dict) else stripe.Price.retrieve(item["price"])
        if price["product"] == SURCHARGE_PRODUCT_ID:
            continue
        total += (price.get("unit_amount") or 0) * (item.get("quantity") or 1)
    return round(total * SURCHARGE_RATE)


def get_or_create_surcharge_price(amount_cents, interval):
    """
    Returns an existing recurring surcharge price for the given amount,
    or creates a new one if none exists.
    """
    cache_key = f"{amount_cents}_{interval}"
    if cache_key in price_cache:
        return price_cache[cache_key]

    existing = stripe.Price.list(product=SURCHARGE_PRODUCT_ID, active=True, limit=100)
    for p in existing["data"]:
        if (
            p["unit_amount"] == amount_cents
            and p.get("recurring", {}).get("interval") == interval
            and p["currency"] == "usd"
        ):
            price_cache[cache_key] = p["id"]
            return p["id"]

    if DRY_RUN:
        placeholder = f"[would-create-{amount_cents}cents-{interval}]"
        price_cache[cache_key] = placeholder
        return placeholder

    new_price = stripe.Price.create(
        product=SURCHARGE_PRODUCT_ID,
        unit_amount=amount_cents,
        currency="usd",
        recurring={"interval": interval},
        nickname=f"CC Surcharge ${amount_cents / 100:.2f}/{interval}",
    )
    price_cache[cache_key] = new_price["id"]
    return new_price["id"]


def main():
    print("=" * 55)
    print("  Stripe CC Surcharge Migration")
    print(f"  Mode: {'DRY RUN (no changes will be made)' if DRY_RUN else 'LIVE'}")
    key = stripe.api_key or ""
    print(f"  Key:  {'PRODUCTION' if key.startswith('sk_live') else 'TEST/SANDBOX'}")
    print("=" * 55)
    print()

    results = {
        "total": 0,
        "surcharged": [],
        "skipped_already_has_surcharge": [],
        "skipped_send_invoice": [],
        "skipped_ach": [],
        "skipped_unknown_pm": [],
        "skipped_zero_amount": [],
        "errors": [],
    }

    # Paginate through all active subscriptions
    params = {
        "status": "active",
        "limit": 100,
        "expand": ["data.items.data.price", "data.default_payment_method"],
    }

    while True:
        page = stripe.Subscription.list(**params)

        for sub in page["data"]:
            results["total"] += 1
            label = f"{sub['id']} (cus: {sub['customer']})"

            try:
                # Skip if surcharge already exists
                if find_surcharge_item(sub):
                    results["skipped_already_has_surcharge"].append(label)
                    continue

                # CRITICAL: Only process auto-charge subscriptions
                if sub.get("collection_method") != "charge_automatically":
                    results["skipped_send_invoice"].append(label)
                    continue

                # Check payment method type
                pm_type = get_payment_method_type(sub)

                if not pm_type:
                    results["skipped_unknown_pm"].append(label)
                    continue

                if pm_type != "card":
                    results["skipped_ach"].append(f"{label} [{pm_type}]")
                    continue

                # Calculate surcharge amount
                surcharge_cents = calculate_surcharge_cents(sub)
                if surcharge_cents <= 0:
                    results["skipped_zero_amount"].append(label)
                    continue

                # Get billing interval from primary item
                primary_item = next(
                    (i for i in sub["items"]["data"]
                     if i["price"]["product"] != SURCHARGE_PRODUCT_ID),
                    None
                )
                interval = primary_item["price"].get("recurring", {}).get("interval", "month") if primary_item else "month"

                # Get or create the surcharge price
                surcharge_price_id = get_or_create_surcharge_price(surcharge_cents, interval)

                # Add surcharge item to subscription
                if not DRY_RUN:
                    stripe.SubscriptionItem.create(
                        subscription=sub["id"],
                        price=surcharge_price_id,
                        quantity=1,
                        proration_behavior="none",
                    )

                display = f"${surcharge_cents / 100:.2f}/{interval}"
                results["surcharged"].append(f"{label} → surcharge {display} (price: {surcharge_price_id})")

            except Exception as e:
                results["errors"].append(f"{label}: {e}")

        if not page["has_more"]:
            break

        params["starting_after"] = page["data"][-1]["id"]

    # Print summary
    print("\n" + "=" * 20 + " RESULTS " + "=" * 20)
    print()
    print(f"Total subscriptions processed:  {results['total']}")
    print(f"Surcharge added:                {len(results['surcharged'])}")
    print(f"Already had surcharge:          {len(results['skipped_already_has_surcharge'])}")
    print(f"Skipped (send invoice):         {len(results['skipped_send_invoice'])}")
    print(f"Skipped (ACH/non-card):         {len(results['skipped_ach'])}")
    print(f"Skipped (unknown PM):           {len(results['skipped_unknown_pm'])}")
    print(f"Skipped (zero amount):          {len(results['skipped_zero_amount'])}")
    print(f"Errors:                         {len(results['errors'])}")

    if results["surcharged"]:
        print("\n-- Surcharged --")
        for r in results["surcharged"]:
            print(f"  {r}")

    if results["skipped_send_invoice"]:
        print("\n-- Skipped (send invoice / not auto-charge) --")
        for r in results["skipped_send_invoice"]:
            print(f"  {r}")

    if results["skipped_ach"]:
        print("\n-- Skipped ACH/non-card --")
        for r in results["skipped_ach"]:
            print(f"  {r}")

    if results["skipped_unknown_pm"]:
        print("\n-- Skipped (unknown payment method) --")
        print("  These have no payment method on file. Review manually in Stripe.")
        for r in results["skipped_unknown_pm"]:
            print(f"  {r}")

    if results["errors"]:
        print("\n-- Errors --")
        for r in results["errors"]:
            print(f"  {r}")

    if DRY_RUN:
        print("\nDRY RUN COMPLETE — no changes were made.")
        print("Re-run without --dry-run to apply.\n")
    else:
        print("\nMigration complete.\n")


if __name__ == "__main__":
    main()
