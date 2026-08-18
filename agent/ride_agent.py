from __future__ import annotations

from typing import Literal

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.llm import ToolError

from .prompt import build_instructions
from .state import BookingState, GuardError
from .tools_client import ToolsAPIError, ToolsClient


def _tool_error(exc: Exception) -> ToolError:
    if isinstance(exc, (GuardError, ToolsAPIError)):
        return ToolError(str(exc))
    return ToolError("That action could not be completed. Offer to retry or transfer.")


class RideBookingAgent(Agent):
    """LiveKit agent whose tools enforce the transactional state machine."""

    def __init__(self, state: BookingState, client: ToolsClient, context: dict) -> None:
        self.state = state
        self.client = client
        super().__init__(instructions=build_instructions(context))

    async def on_enter(self) -> None:
        if self.state.rider_id and self.state.first_name:
            greeting = (
                f"Hi {self.state.first_name}, thanks for calling RideCo. "
                "Where should the driver pick you up?"
            )
        else:
            greeting = (
                "Hi, thanks for calling RideCo. I can help book a ride. "
                "What name should I use?"
            )
        await self.session.generate_reply(
            instructions=f'Say exactly this greeting: "{greeting}"',
            allow_interruptions=True,
        )

    @function_tool()
    async def create_guest_rider(self, first_name: str) -> dict:
        """Create a local guest rider after an unknown caller gives their first name."""
        try:
            result = await self.client.call(
                "create_guest_rider", phone=self.state.caller_ani, first_name=first_name
            )
            self.state.rider_id = result["rider_id"]
            self.state.first_name = first_name
            self.state.rider_status = "active"
            return {"created": True, "first_name": first_name}
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def get_saved_places(self) -> dict:
        """Get saved place labels for a recognized caller; never use for guests."""
        if self.state.auth_level == "anonymous" or not self.state.rider_id:
            raise ToolError("Saved places are unavailable for an unrecognized caller.")
        try:
            return await self.client.call("get_saved_places", rider_id=self.state.rider_id)
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def get_recent_dropoffs(self, limit: int = 3) -> dict:
        """Get recent destinations for a recognized caller, up to three."""
        if self.state.auth_level == "anonymous" or not self.state.rider_id:
            raise ToolError("Recent trips are unavailable for an unrecognized caller.")
        try:
            return await self.client.call(
                "get_recent_dropoffs", rider_id=self.state.rider_id, limit=min(limit, 3)
            )
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def geocode_address(
        self, query: str, address_kind: Literal["pickup", "dropoff"]
    ) -> dict:
        """Find candidates for a spoken pickup or dropoff. Read one back before confirming it."""
        try:
            result = await self.client.call(
                "geocode_address", query=query, market=self.state.default_market
            )
            self.state.remember_geocode(address_kind, result.get("candidates", []))
            return result
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def confirm_address(
        self, address_kind: Literal["pickup", "dropoff"], place_id: str
    ) -> dict:
        """Record that the caller explicitly confirmed one latest geocoded candidate."""
        try:
            candidate = self.state.confirm_address(address_kind, place_id)
            return {
                "confirmed": True,
                "address_kind": address_kind,
                "formatted_address": candidate["formatted_address"],
            }
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def get_ride_options(self) -> dict:
        """Get fresh fares, pickup ETAs, availability, distance, and surge after both addresses are confirmed."""
        try:
            if not self.state.pickup or not self.state.dropoff:
                raise GuardError(
                    "Confirm pickup and destination before requesting options."
                )
            result = await self.client.call(
                "get_ride_options",
                pickup_place_id=self.state.pickup["place_id"],
                dropoff_place_id=self.state.dropoff["place_id"],
                rider_id=self.state.rider_id,
                accessibility_needs=self.state.accessibility_needs,
            )
            self.state.remember_quote(result)
            return result
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def select_ride_option(self, product_id: str) -> dict:
        """Freeze one available product and its fare range from the latest quote."""
        try:
            option = self.state.select_product(product_id)
            return {"selected": True, "option": option}
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def get_payment_methods(self) -> dict:
        """Get safe payment metadata and cash support; never returns full card details."""
        if not self.state.rider_id:
            return {
                "methods": [],
                "ride_cash_balance": 0,
                "cash_supported_in_market": self.state.cash_supported,
            }
        try:
            result = await self.client.call(
                "get_payment_methods", rider_id=self.state.rider_id
            )
            self.state.payment_methods = result.get("methods", [])
            self.state.ride_cash_balance = float(result.get("ride_cash_balance", 0))
            self.state.cash_supported = bool(result.get("cash_supported_in_market"))
            return result
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def send_otp(self) -> dict:
        """Send an SMS verification code to the caller before using a saved card."""
        try:
            return await self.client.call("send_otp", phone=self.state.caller_ani)
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def verify_otp(self, code: str) -> dict:
        """Verify the caller's SMS code. Never infer or guess a code."""
        try:
            result = await self.client.call(
                "verify_otp", phone=self.state.caller_ani, code=code
            )
            if result.get("verified"):
                self.state.auth_level = "otp_verified"
            return result
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def select_payment_method(self, payment_method: str) -> dict:
        """Select saved_card:<id>, ride_cash, cash, or pay_link after explaining it."""
        try:
            self.state.select_payment(payment_method)
            return {"selected": True, "payment": payment_method}
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def send_payment_link_sms(self) -> dict:
        """Send a secure payment link. Use this instead of collecting card data by voice."""
        try:
            high = (
                float(self.state.selected_option["fare_high"])
                if self.state.selected_option
                else None
            )
            result = await self.client.call(
                "send_payment_link_sms",
                phone=self.state.caller_ani,
                amount_estimate=high,
            )
            self.state.payment_link_ready = result.get("status") == "ready"
            return result
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def check_payment_link_status(self) -> dict:
        """Check whether the caller completed the secure payment link before selecting pay_link."""
        try:
            result = await self.client.call(
                "get_payment_link_status", phone=self.state.caller_ani
            )
            self.state.payment_link_ready = result.get("status") == "ready"
            return result
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def prepare_booking_confirmation(self) -> dict:
        """Create the exact final trip read-back and one-time token. Read the summary, then ask for an explicit yes."""
        try:
            token, summary = self.state.prepare_confirmation()
            return {"confirmation_token": token, "summary_to_read": summary}
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def book_ride(
        self,
        context: RunContext,
        confirmation_token: str,
        caller_explicitly_confirmed: bool,
    ) -> dict:
        """Book only after reading the prepared summary and hearing an explicit yes from the caller."""
        context.disallow_interruptions()
        try:
            self.state.authorize_booking(confirmation_token, caller_explicitly_confirmed)
            snapshot = self.state.snapshot()
            result = await self.client.call(
                "book_ride",
                rider_id=self.state.rider_id,
                pickup_place_id=snapshot["pickup_place_id"],
                dropoff_place_id=snapshot["dropoff_place_id"],
                product_id=snapshot["product_id"],
                payment_method=snapshot["payment_method"],
                quoted_fare_low=snapshot["fare_low"],
                quoted_fare_high=snapshot["fare_high"],
                idempotency_key=confirmation_token,
            )
            self.state.booking_ref = result.get("booking_ref")
            return result
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def get_booking_status(self) -> dict:
        """Get current driver and pickup status for the booking from this call."""
        if not self.state.booking_ref:
            raise ToolError("There is no booking in this call yet.")
        try:
            return await self.client.call(
                "get_booking_status", booking_ref=self.state.booking_ref
            )
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def get_cancellation_quote(self) -> dict:
        """Get the cancellation fee to disclose before asking for cancellation consent."""
        if not self.state.booking_ref:
            raise ToolError("There is no booking in this call yet.")
        try:
            return await self.client.call(
                "get_cancellation_quote", booking_ref=self.state.booking_ref
            )
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def cancel_ride(
        self, context: RunContext, reason: str, caller_explicitly_confirmed: bool
    ) -> dict:
        """Cancel after disclosing the fee and receiving an explicit yes."""
        if not caller_explicitly_confirmed:
            raise ToolError("The caller must explicitly confirm cancellation.")
        if not self.state.booking_ref:
            raise ToolError("There is no booking in this call yet.")
        context.disallow_interruptions()
        try:
            return await self.client.call(
                "cancel_ride", booking_ref=self.state.booking_ref, reason=reason
            )
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def send_confirmation_sms(self) -> dict:
        """Text the matched ride details after a booking succeeds."""
        if not self.state.booking_ref:
            raise ToolError("There is no booking to text yet.")
        try:
            return await self.client.call(
                "send_confirmation_sms",
                phone=self.state.caller_ani,
                booking_ref=self.state.booking_ref,
            )
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def transfer_to_human(self, reason: str) -> dict:
        """Request a human handoff for unsupported, account-blocked, safety, or failed-service cases."""
        try:
            return await self.client.call("transfer_to_human", reason=reason)
        except Exception as exc:
            raise _tool_error(exc) from exc
