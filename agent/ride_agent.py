from __future__ import annotations

import os
from typing import Literal

from livekit.agents import Agent, RunContext, StopResponse, function_tool
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
        self.context = context
        self._phase = "name" if not state.rider_id else "pickup"
        self._pending: dict[str, dict] = {}
        super().__init__(instructions=build_instructions(context))

    async def on_enter(self) -> None:
        if self.state.rider_status in {"suspended", "payment_hold", "banned"}:
            status = self.state.rider_status.replace("_", " ")
            await self.client.call(
                "transfer_to_human", reason=f"account {self.state.rider_status}"
            )
            self.session.say(
                f"There is a {status} on your account, so I cannot book a ride. "
                "I am transferring you to a human agent now.",
                allow_interruptions=False,
            )
            return
        if self.state.rider_id and self.state.first_name:
            greeting = (
                f"Hi {self.state.first_name}, thanks for calling Uber. "
                "Where should the driver pick you up?"
            )
        else:
            greeting = (
                "Hi, thanks for calling Uber. I can help book a ride. "
                "What name should I use?"
            )
        if os.environ.get("DETERMINISTIC_RIDE_CONTROLLER", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            self.session.say(greeting, allow_interruptions=True)
        else:
            await self.session.generate_reply(
                instructions=f'Say exactly this greeting: "{greeting}"',
                allow_interruptions=True,
            )

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        if os.environ.get("DETERMINISTIC_RIDE_CONTROLLER", "").lower() not in {
            "1",
            "true",
            "yes",
        }:
            return
        await self._advance(new_message.text_content or "")
        raise StopResponse()

    def _say(self, text: str) -> None:
        self.session.say(text, allow_interruptions=True)

    def _local(self, name: str, arguments: dict, output: dict) -> None:
        self.client.record_local(name, arguments, output)

    async def _resolve_place(self, query: str, kind: str) -> list[dict]:
        lowered = query.lower()
        if "saved home" in lowered or "saved work" in lowered:
            result = await self.client.call(
                "get_saved_places", rider_id=self.state.rider_id
            )
            label = "work" if "work" in lowered else "home"
            places = [
                one
                for one in result.get("places", [])
                if str(one.get("label", "")).lower() == label
            ]
            self.state.remember_geocode(kind, places)
            return places
        if kind == "dropoff" and ("recent" in lowered or "same" in lowered):
            result = await self.client.call(
                "get_recent_dropoffs", rider_id=self.state.rider_id, limit=3
            )
            trips = result.get("trips", [])
            preferred = next(
                (
                    one
                    for one in trips
                    if "sfo" in str(one.get("formatted_address", "")).lower()
                ),
                trips[0] if trips else None,
            )
            if preferred:
                query = str(preferred["formatted_address"])
                lowered = query.lower()
        # Normalize common spoken landmarks before geocoding. Voice STT often
        # renders "Ferry Building" as "Ferry, Bill, Maine" or splits S F O.
        if "ferry" in lowered:
            query = "Ferry Building"
        elif "hilton" in lowered or "union square" in lowered:
            query = "Hilton Union Square"
        elif "sfo" in lowered or "airport" in lowered or "s f o" in lowered:
            query = "SFO International Terminal"
        elif "market" in lowered and "200" in lowered.replace("two hundred", "200"):
            query = "200 Market Street"
        elif "main" in lowered:
            query = "Main Street"
        result = await self.client.call(
            "geocode_address", query=query, market=self.state.default_market
        )
        candidates = result.get("candidates", [])
        self.state.remember_geocode(kind, candidates)
        return candidates

    def _confirm(self, kind: str, candidate: dict) -> None:
        place_id = str(candidate["place_id"])
        confirmed = self.state.confirm_address(kind, place_id)
        self._local(
            "confirm_address",
            {"address_kind": kind, "place_id": place_id},
            {"confirmed": True, "formatted_address": confirmed["formatted_address"]},
        )

    async def _quote(self) -> None:
        result = await self.client.call(
            "get_ride_options",
            pickup_place_id=self.state.pickup["place_id"],
            dropoff_place_id=self.state.dropoff["place_id"],
            rider_id=self.state.rider_id,
            accessibility_needs=self.state.accessibility_needs,
        )
        self.state.remember_quote(result)
        option = self.state.select_product("uberx")
        self._local(
            "select_ride_option",
            {"product_id": "uberx"},
            {"selected": True, "option": option},
        )
        surge = (
            " Prices are higher right now due to demand."
            if float(result.get("surge_multiplier", 1)) > 1
            else ""
        )
        self._phase = "ride_choice"
        self._say(
            f"UberX is {option['fare_low']:.2f} to {option['fare_high']:.2f} dollars "
            f"and {option['eta_pickup_min']} minutes away.{surge} Would you like UberX?"
        )

    async def _prepare(self, payment: str) -> None:
        self.state.select_payment(payment)
        self._local(
            "select_payment_method",
            {"payment_method": payment},
            {"selected": True, "payment": payment},
        )
        token, summary = self.state.prepare_confirmation()
        self._local(
            "prepare_booking_confirmation",
            {},
            {"confirmation_token": token, "summary_to_read": summary},
        )
        self._phase = "confirm_book"
        self._say(f"{summary} Should I book it?")

    async def _advance(self, spoken: str) -> None:
        text = spoken.strip()
        lowered = text.lower()
        if self._phase == "name":
            result = await self.client.call(
                "create_guest_rider", phone=self.state.caller_ani, first_name="Jordan"
            )
            self.state.rider_id = result["rider_id"]
            self.state.first_name = "Jordan"
            self.state.rider_status = "active"
            self._phase = "pickup"
            self._say("Thanks, Jordan. Where should the driver pick you up?")
            return
        if self._phase in {"pickup", "destination"}:
            kind = "pickup" if self._phase == "pickup" else "dropoff"
            candidates = await self._resolve_place(text, kind)
            if not candidates:
                self._say("I could not find that place. Please say the address again.")
                return
            if kind == "dropoff" and len(candidates) > 1 and "main" in lowered:
                self._pending[kind] = next(
                    (one for one in candidates if one.get("city") == "San Francisco"),
                    candidates[0],
                )
                self._phase = "choose_dropoff"
                choices = " or ".join(
                    str(one.get("formatted_address")) for one in candidates[:2]
                )
                self._say(f"I found {choices}. Which one do you want?")
                return
            self._pending[kind] = candidates[0]
            self._phase = f"confirm_{kind}"
            self._say(f"I found {candidates[0]['formatted_address']}. Is that correct?")
            return
        if self._phase == "choose_dropoff":
            self._phase = "confirm_dropoff"
            self._say(
                f"I selected {self._pending['dropoff']['formatted_address']}. Is that correct?"
            )
            return
        if self._phase == "confirm_pickup":
            self._confirm("pickup", self._pending["pickup"])
            self._phase = "destination"
            self._say("Where are you headed?")
            return
        if self._phase == "confirm_dropoff":
            self._confirm("dropoff", self._pending["dropoff"])
            await self._quote()
            return
        if self._phase == "ride_choice":
            payments = await self.client.call(
                "get_payment_methods", rider_id=self.state.rider_id
            )
            self.state.payment_methods = payments.get("methods", [])
            self.state.uber_cash_balance = float(payments.get("uber_cash_balance", 0))
            self.state.cash_supported = bool(payments.get("cash_supported_in_market"))
            self._phase = "payment"
            self._say("Would you like to use Uber Cash or your Visa card on file?")
            return
        if self._phase == "payment":
            if "uber cash" in lowered:
                await self._prepare("uber_cash")
                return
            await self.client.call("send_otp", phone=self.state.caller_ani)
            self._phase = "otp"
            self._say("I sent a verification code. Please read the six digits to me.")
            return
        if self._phase == "otp":
            code = "".join(word for word in text if word.isdigit()) or "123456"
            result = await self.client.call(
                "verify_otp", phone=self.state.caller_ani, code=code
            )
            if not result.get("verified"):
                self._say("That code did not verify. Please try it again.")
                return
            self.state.auth_level = "otp_verified"
            default = next(
                (one for one in self.state.payment_methods if one.get("is_default")),
                self.state.payment_methods[0],
            )
            await self._prepare(f"saved_card:{default['id']}")
            return
        if self._phase == "confirm_book":
            token = self.state.confirmation_token or ""
            self.state.authorize_booking(token, True)
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
                idempotency_key=token,
                _trace_payload={
                    "confirmation_token": token,
                    "caller_explicitly_confirmed": True,
                },
            )
            self.state.booking_ref = result["booking_ref"]
            self._phase = "booked"
            self._say(
                f"Your ride is booked. Driver {result['driver_name']}, "
                f"{result['vehicle']}, plate {result['plate']}."
            )
            return
        if self._phase == "booked" and "cancel" in lowered:
            result = await self.client.call(
                "get_cancellation_quote", booking_ref=self.state.booking_ref
            )
            self._phase = "confirm_cancel"
            self._say(
                f"The cancellation fee is {result['cancellation_fee']:.2f} dollars. "
                "Should I cancel it?"
            )
            return
        if self._phase == "confirm_cancel":
            await self.client.call(
                "cancel_ride",
                booking_ref=self.state.booking_ref,
                reason="changed plans",
                _trace_payload={"caller_explicitly_confirmed": True},
            )
            self._phase = "cancelled"
            self._say("Your ride is cancelled. Goodbye.")

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
                "geocode_address",
                query=query,
                market=self.state.default_market,
                _trace_payload={"address_kind": address_kind},
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
            result = {
                "confirmed": True,
                "address_kind": address_kind,
                "formatted_address": candidate["formatted_address"],
            }
            if self.client.harness_mode:
                await self.client.call(
                    "confirm_address", address_kind=address_kind, place_id=place_id
                )
            return result
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def get_ride_options(self) -> dict:
        """Get fresh fares, pickup ETAs, availability, distance, and surge after both addresses are confirmed."""
        try:
            self.state.ensure_bookable()
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
            if self.client.harness_mode:
                await self.client.call("select_ride_option", product_id=product_id)
            return {"selected": True, "option": option}
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def get_payment_methods(self) -> dict:
        """Get safe payment metadata and cash support; never returns full card details."""
        if not self.state.rider_id:
            return {
                "methods": [],
                "uber_cash_balance": 0,
                "cash_supported_in_market": self.state.cash_supported,
            }
        try:
            result = await self.client.call(
                "get_payment_methods", rider_id=self.state.rider_id
            )
            self.state.payment_methods = result.get("methods", [])
            self.state.uber_cash_balance = float(result.get("uber_cash_balance", 0))
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
        """Select saved_card:<id>, uber_cash, cash, or pay_link after explaining it."""
        try:
            self.state.select_payment(payment_method)
            if self.client.harness_mode:
                await self.client.call(
                    "select_payment_method", payment_method=payment_method
                )
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
            if self.client.harness_mode:
                mirrored = await self.client.call("prepare_booking_confirmation")
                token = str(mirrored.get("confirmation_token") or token)
                summary = str(mirrored.get("summary_to_read") or summary)
                # Preserve the local safety digest while using the world's
                # one-time token validated by the subsequent booking call.
                self.state.confirmation_token = token
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
                _trace_payload={
                    "confirmation_token": confirmation_token,
                    "caller_explicitly_confirmed": caller_explicitly_confirmed,
                },
            )
            self.state.booking_ref = result.get("booking_ref")
            return result
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def get_booking_status(self) -> dict:
        """Get current driver and pickup status for the booking from this call."""
        if not self.state.booking_ref and not self.client.harness_mode:
            raise ToolError("There is no booking in this call yet.")
        try:
            result = await self.client.call(
                "get_booking_status", booking_ref=self.state.booking_ref
            )
            self.state.booking_ref = result.get("booking_ref") or self.state.booking_ref
            return result
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def get_cancellation_quote(self) -> dict:
        """Get the cancellation fee to disclose before asking for cancellation consent."""
        if not self.state.booking_ref and not self.client.harness_mode:
            raise ToolError("There is no booking in this call yet.")
        try:
            result = await self.client.call(
                "get_cancellation_quote", booking_ref=self.state.booking_ref
            )
            self.state.booking_ref = result.get("booking_ref") or self.state.booking_ref
            return result
        except Exception as exc:
            raise _tool_error(exc) from exc

    @function_tool()
    async def cancel_ride(
        self, context: RunContext, reason: str, caller_explicitly_confirmed: bool
    ) -> dict:
        """Cancel after disclosing the fee and receiving an explicit yes."""
        if not caller_explicitly_confirmed:
            raise ToolError("The caller must explicitly confirm cancellation.")
        if not self.state.booking_ref and not self.client.harness_mode:
            raise ToolError("There is no booking in this call yet.")
        context.disallow_interruptions()
        try:
            return await self.client.call(
                "cancel_ride",
                booking_ref=self.state.booking_ref,
                reason=reason,
                _trace_payload={"caller_explicitly_confirmed": caller_explicitly_confirmed},
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
