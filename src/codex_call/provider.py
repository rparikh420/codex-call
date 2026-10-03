"""LiveKit SDK boundary. Dispatch metadata deliberately contains only the call ID."""

import json
import math
from urllib.parse import urlsplit

from google.protobuf.duration_pb2 import Duration
from livekit import api


class ProviderError(Exception):
    """Safe external-provider failure; never includes remote response bodies."""


def validate_livekit(settings):
    try:
        url = urlsplit(settings.livekit_url or "")
        values = (
            settings.livekit_key.get_secret_value() if settings.livekit_key else "",
            settings.livekit_secret.get_secret_value() if settings.livekit_secret else "",
            settings.sip_trunk_id or "",
        )
        valid = (
            url.scheme in {"http", "https", "ws", "wss"}
            and url.hostname
            and all(value.strip() for value in values)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ProviderError("missing_livekit_configuration")


class LiveKitProvider:
    def __init__(self, settings):
        validate_livekit(settings)
        self.settings = settings
        self._api = None

    @property
    def client(self):
        if self._api is None:
            self._api = api.LiveKitAPI(
                self.settings.livekit_url,
                self.settings.livekit_key.get_secret_value(),
                self.settings.livekit_secret.get_secret_value(),
            )
        return self._api

    async def dispatch(self, call):
        if call["mode"] != "live":
            raise ProviderError("call_mode_mismatch")
        try:
            await self.client.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=self.settings.agent_name,
                    room=call["room_name"],
                    metadata=json.dumps({"call_id": call["id"]}),
                )
            )
        except Exception:
            raise ProviderError("dispatch_failed") from None

    async def dial(self, call, remaining_s):
        duration = max(
            1, math.floor(min(remaining_s, call["max_duration_s"], self.settings.max_duration_s))
        )
        request = api.CreateSIPParticipantRequest(
            sip_trunk_id=self.settings.sip_trunk_id,
            sip_call_to=call["to_number"],
            room_name=call["room_name"],
            participant_identity="phone-" + call["id"],
            wait_until_answered=True,
            ringing_timeout=Duration(seconds=min(30, duration)),
            max_call_duration=Duration(seconds=duration),
        )
        try:
            return await self.client.sip.create_sip_participant(request, timeout=remaining_s)
        except Exception:
            raise ProviderError("provider_failed") from None

    async def hangup(self, call):
        try:
            await self.client.room.delete_room(api.DeleteRoomRequest(room=call["room_name"]))
        except api.TwirpError as exc:
            if exc.code != "not_found":
                raise ProviderError("cleanup_failed") from None
        except Exception:
            raise ProviderError("cleanup_failed") from None

    async def aclose(self):
        if self._api is not None:
            await self._api.aclose()
            self._api = None
