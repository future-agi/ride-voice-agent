from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Literal


class GuardError(ValueError):
    """Raised when a money-moving action is not safe yet."""


AddressKind = Literal["pickup", "dropoff"]


@dataclass
class BookingState:
    caller_ani: str
    session_id: str
    auth_level: str = "anonymous"
    rider_id: str | None = None
    first_name: str | None = None
    rider_status: str = "unknown"
    default_market: str | None = None
    accessibility_needs: list[str] = field(default_factory=list)
    cash_supported: bool = False
    max_fare_without_otp: float | None = None
    payment_methods: list[dict[str, Any]] = field(default_factory=list)
    ride_cash_balance: float = 0.0
    candidates: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {"pickup": [], "dropoff": []}
    )
    pickup: dict[str, Any] | None = None
    dropoff: dict[str, Any] | None = None
    quote: dict[str, Any] | None = None
    selected_option: dict[str, Any] | None = None
    selected_product_id: str | None = None
    payment_method_selected: str | None = None
    payment_link_ready: bool = False
    confirmation_token: str | None = None
    confirmation_digest: str | None = None
    booking_ref: str | None = None

    def set_identity(self, identity: dict[str, Any]) -> None:
        self.rider_id = identity.get("rider_id")
        self.first_name = identity.get("first_name")
        self.rider_status = identity.get("status") or "unknown"
        self.default_market = identity.get("default_market")
        self.accessibility_needs = identity.get("accessibility_needs") or []
        self.cash_supported = bool(identity.get("cash_supported_in_market"))
        self.auth_level = "ani_matched" if self.rider_id else "anonymous"

    def remember_geocode(
        self, address_kind: AddressKind, candidates: list[dict[str, Any]]
    ) -> None:
        if address_kind not in ("pickup", "dropoff"):
            raise GuardError("Address kind must be pickup or dropoff.")
        self.candidates[address_kind] = candidates

    def confirm_address(self, address_kind: AddressKind, place_id: str) -> dict[str, Any]:
        candidate = next(
            (c for c in self.candidates[address_kind] if c.get("place_id") == place_id),
            None,
        )
        if not candidate:
            raise GuardError("That address was not in the latest geocoding results.")
        previous = self.pickup if address_kind == "pickup" else self.dropoff
        if address_kind == "pickup":
            self.pickup = candidate
        else:
            self.dropoff = candidate
        if previous and previous.get("place_id") != place_id:
            self._invalidate_quote()
        return candidate

    def remember_quote(self, quote: dict[str, Any]) -> None:
        if not self.pickup or not self.dropoff:
            raise GuardError("Confirm pickup and destination before requesting options.")
        self.quote = quote
        self.selected_option = None
        self.selected_product_id = None
        self.payment_method_selected = None
        self._clear_confirmation()

    def select_product(self, product_id: str) -> dict[str, Any]:
        if not self.quote:
            raise GuardError("Get a current ride quote first.")
        option = next(
            (o for o in self.quote.get("options", []) if o.get("product_id") == product_id),
            None,
        )
        if not option or not option.get("is_available"):
            raise GuardError("That ride option is not available in the current quote.")
        self.selected_option = option
        self.selected_product_id = product_id
        self.payment_method_selected = None
        self._clear_confirmation()
        return option

    def select_payment(self, payment_method: str) -> None:
        if not self.selected_option:
            raise GuardError("Select a quoted ride option before payment.")
        high = float(self.selected_option["fare_high"])
        if payment_method.startswith("saved_card:"):
            method_id = payment_method.partition(":")[2]
            method = next(
                (m for m in self.payment_methods if m.get("id") == method_id), None
            )
            if not method or not method.get("is_valid") or method.get("is_expired"):
                raise GuardError("That saved payment method is not valid.")
            if self.auth_level != "otp_verified":
                raise GuardError("A successful OTP check is required for a saved card.")
        elif payment_method == "ride_cash":
            if self.ride_cash_balance < high:
                raise GuardError(
                    "The RideCo Cash balance does not cover the high fare estimate."
                )
        elif payment_method == "cash":
            if not self.cash_supported:
                raise GuardError("Cash is not supported in this market.")
            if (
                self.auth_level != "otp_verified"
                and self.max_fare_without_otp is not None
                and high > self.max_fare_without_otp
            ):
                raise GuardError("This fare is above the unverified cash fare cap.")
        elif payment_method == "pay_link":
            if not self.payment_link_ready:
                raise GuardError("The payment link is still pending.")
        else:
            raise GuardError("Unsupported payment method.")
        self.payment_method_selected = payment_method
        self._clear_confirmation()

    def snapshot(self) -> dict[str, Any]:
        if not self.pickup or not self.dropoff or not self.selected_option:
            raise GuardError("The trip is not fully selected.")
        if not self.payment_method_selected:
            raise GuardError("Select a settled payment method before confirmation.")
        return {
            "pickup_place_id": self.pickup["place_id"],
            "dropoff_place_id": self.dropoff["place_id"],
            "product_id": self.selected_option["product_id"],
            "fare_low": float(self.selected_option["fare_low"]),
            "fare_high": float(self.selected_option["fare_high"]),
            "currency": self.quote.get("currency", "USD") if self.quote else "USD",
            "surge_multiplier": float(self.quote.get("surge_multiplier", 1.0))
            if self.quote
            else 1.0,
            "payment_method": self.payment_method_selected,
        }

    def prepare_confirmation(self) -> tuple[str, str]:
        snapshot = self.snapshot()
        raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        self.confirmation_digest = hashlib.sha256(raw.encode()).hexdigest()
        self.confirmation_token = secrets.token_urlsafe(12)
        payment = self._payment_label()
        option = self.selected_option or {}
        summary = (
            f"{option.get('display_name')}, {snapshot['fare_low']:.2f} to "
            f"{snapshot['fare_high']:.2f} {snapshot['currency']}, from "
            f"{self.pickup['formatted_address']} to {self.dropoff['formatted_address']}, "
            f"paying with {payment}."
        )
        if snapshot["surge_multiplier"] > 1:
            summary += " Prices are higher right now due to demand."
        return self.confirmation_token, summary

    def authorize_booking(self, token: str, caller_explicitly_confirmed: bool) -> None:
        if not caller_explicitly_confirmed:
            raise GuardError("The caller must give an explicit yes before booking.")
        if not self.confirmation_token or token != self.confirmation_token:
            raise GuardError("The confirmation token does not match the current trip.")
        snapshot = self.snapshot()
        raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        if digest != self.confirmation_digest:
            raise GuardError("The trip changed after confirmation; read it back again.")
        self._clear_confirmation()

    def _payment_label(self) -> str:
        selected = self.payment_method_selected or "unknown"
        if selected.startswith("saved_card:"):
            method_id = selected.partition(":")[2]
            method = next((m for m in self.payment_methods if m.get("id") == method_id), {})
            return f"{method.get('brand', 'card')} ending {method.get('last4', 'unknown')}"
        return selected.replace("_", " ")

    def _invalidate_quote(self) -> None:
        self.quote = None
        self.selected_option = None
        self.selected_product_id = None
        self.payment_method_selected = None
        self._clear_confirmation()

    def _clear_confirmation(self) -> None:
        self.confirmation_token = None
        self.confirmation_digest = None
