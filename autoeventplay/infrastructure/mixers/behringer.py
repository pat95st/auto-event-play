"""Behringer / Midas consoles (OSC over UDP).

* WING (Full, Compact, Rack)                         – port 2223
* X32 / M32 family (X32, Compact, Producer, Rack, Core, M32, M32R, M32C, M32 Live)  – port 10023
* X-AIR / M-AIR family (XR12, XR16, XR18, X18, MR12, MR18) – port 10024
"""

from __future__ import annotations

from ...domain.model import Target, TargetKind
from .base import KindSpec, MixerError, register_driver
from ...i18n import Msg
from .osc_base import (
    OscMixerDriver,
    OscParam,
    decode_level_db,
    decode_level_x32,
    decode_mute_direct,
    decode_on,
    level_db,
    level_x32,
    mute_direct,
    mute_via_on,
)

K = TargetKind


# =========================================================================== X32 / M32


@register_driver
class BehringerX32Driver(OscMixerDriver):
    type_id = "behringer_x32"
    display_name = "Behringer X32 / Midas M32"
    family = "Behringer / Midas"
    default_port = 10023
    kinds = [
        KindSpec(K.CHANNEL, "Channel", 32),
        KindSpec(K.STEREO_IN, "Aux In", 8),
        KindSpec(K.FX_RETURN, "FX Return", 8),
        KindSpec(K.BUS, "Mix bus", 16),
        KindSpec(K.MATRIX, "Matrix", 6),
        KindSpec(K.MAIN, "Main", 2, number_labels={1: "Main LR", 2: "Main mono (M/C)"}),
        KindSpec(K.DCA, "DCA", 8),
        KindSpec(K.MUTE_GROUP, "Mute group", 6, level=False),
        KindSpec(K.SEND, "Channel send", 32, dest_label="Mix bus", dest_max=16),
        KindSpec(K.RAW, "Raw OSC address"),
    ]
    notes = "All models of the X32/M32 series use the same OSC commands. Levels are converted to the X32 fader curve."

    _bases = {K.CHANNEL: "/ch/{n:02d}", K.STEREO_IN: "/auxin/{n:02d}", K.FX_RETURN: "/fxrtn/{n:02d}",
              K.BUS: "/bus/{n:02d}", K.MATRIX: "/mtx/{n:02d}"}

    def param(self, target: Target, which: str) -> OscParam:
        n = target.number or 1
        if target.kind in self._bases:
            base = self._bases[target.kind].format(n=n)
        elif target.kind == K.MAIN:
            base = "/main/st" if n == 1 else "/main/m"
        elif target.kind == K.DCA:
            return OscParam(f"/dca/{n}/on", mute_via_on, decode_on) if which == "mute" else \
                OscParam(f"/dca/{n}/fader", level_x32, decode_level_x32)
        elif target.kind == K.MUTE_GROUP:
            return OscParam(f"/config/mute/{n}", mute_direct, decode_mute_direct)
        elif target.kind == K.SEND:
            base = f"/ch/{n:02d}/mix/{(target.dest or 1):02d}"
            return OscParam(base + "/on", mute_via_on, decode_on) if which == "mute" else \
                OscParam(base + "/level", level_x32, decode_level_x32)
        else:
            raise MixerError(Msg("not supported: {kind}", kind=target.kind.value))
        return OscParam(base + "/mix/on", mute_via_on, decode_on) if which == "mute" else \
            OscParam(base + "/mix/fader", level_x32, decode_level_x32)


# =========================================================================== X-AIR / M-AIR


@register_driver
class BehringerXAirDriver(OscMixerDriver):
    type_id = "behringer_xair"
    display_name = "Behringer X-AIR / Midas M-AIR"
    family = "Behringer / Midas"
    default_port = 10024
    kinds = [
        KindSpec(K.CHANNEL, "Channel", 16),
        KindSpec(K.STEREO_IN, "Aux/USB return", 1),
        KindSpec(K.FX_RETURN, "FX Return", 4),
        KindSpec(K.BUS, "Bus", 6),
        KindSpec(K.FX_SEND, "FX send", 4),
        KindSpec(K.MAIN, "Main LR", 1),
        KindSpec(K.DCA, "DCA", 4),
        KindSpec(K.MUTE_GROUP, "Mute group", 4, level=False),
        KindSpec(K.SEND, "Channel send", 16, mute=False, dest_label="Bus/FX", dest_max=10,
                 dest_labels={1: "Bus 1", 2: "Bus 2", 3: "Bus 3", 4: "Bus 4", 5: "Bus 5", 6: "Bus 6",
                              7: "FX 1", 8: "FX 2", 9: "FX 3", 10: "FX 4"}),
        KindSpec(K.RAW, "Raw OSC address"),
    ]
    notes = "Applies to XR12, XR16, XR18, X18, MR12 and MR18 (same OSC commands). XR12/XR16 have fewer channels."

    def param(self, target: Target, which: str) -> OscParam:
        n = target.number or 1
        if target.kind == K.CHANNEL:
            base = f"/ch/{n:02d}"
        elif target.kind == K.STEREO_IN:
            base = "/rtn/aux"
        elif target.kind == K.FX_RETURN:
            base = f"/rtn/{n}"
        elif target.kind == K.BUS:
            base = f"/bus/{n}"
        elif target.kind == K.FX_SEND:
            base = f"/fxsend/{n}"
        elif target.kind == K.MAIN:
            base = "/lr"
        elif target.kind == K.DCA:
            return OscParam(f"/dca/{n}/on", mute_via_on, decode_on) if which == "mute" else \
                OscParam(f"/dca/{n}/fader", level_x32, decode_level_x32)
        elif target.kind == K.MUTE_GROUP:
            return OscParam(f"/config/mute/{n}", mute_direct, decode_mute_direct)
        elif target.kind == K.SEND:
            return OscParam(f"/ch/{n:02d}/mix/{(target.dest or 1):02d}/level", level_x32, decode_level_x32)
        else:
            raise MixerError(Msg("not supported: {kind}", kind=target.kind.value))
        return OscParam(base + "/mix/on", mute_via_on, decode_on) if which == "mute" else \
            OscParam(base + "/mix/fader", level_x32, decode_level_x32)


# =========================================================================== WING


WING_MIN_DB = -144.0


@register_driver
class BehringerWingDriver(OscMixerDriver):
    type_id = "behringer_wing"
    display_name = "Behringer WING"
    family = "Behringer / Midas"
    default_port = 2223
    kinds = [
        KindSpec(K.CHANNEL, "Channel", 40),
        KindSpec(K.STEREO_IN, "Aux", 8),
        KindSpec(K.BUS, "Bus", 16),
        KindSpec(K.MAIN, "Main", 4),
        KindSpec(K.MATRIX, "Matrix", 8),
        KindSpec(K.DCA, "DCA", 16),
        KindSpec(K.MUTE_GROUP, "Mute group", 8, level=False),
        KindSpec(K.SEND, "Channel send", 40, dest_label="Bus", dest_max=16),
        KindSpec(K.RAW, "Raw OSC address"),
    ]
    notes = "WING, WING Compact and WING Rack (same OSC commands, port 2223). Levels are sent directly in dB."

    _bases = {K.CHANNEL: "/ch/{n}", K.STEREO_IN: "/aux/{n}", K.BUS: "/bus/{n}", K.MAIN: "/main/{n}",
              K.MATRIX: "/mtx/{n}", K.DCA: "/dca/{n}"}

    def param(self, target: Target, which: str) -> OscParam:
        n = target.number or 1
        enc_level = level_db(WING_MIN_DB)
        if target.kind in self._bases:
            base = self._bases[target.kind].format(n=n)
            return OscParam(base + "/mute", mute_direct, decode_mute_direct) if which == "mute" else \
                OscParam(base + "/fdr", enc_level, decode_level_db)
        if target.kind == K.MUTE_GROUP:
            return OscParam(f"/mgrp/{n}/mute", mute_direct, decode_mute_direct)
        if target.kind == K.SEND:
            base = f"/ch/{n}/send/{target.dest or 1}"
            return OscParam(base + "/on", mute_via_on, decode_on) if which == "mute" else \
                OscParam(base + "/lvl", enc_level, decode_level_db)
        raise MixerError(Msg("not supported: {kind}", kind=target.kind.value))

    def _raw_level_param(self, address: str) -> OscParam:
        return OscParam(address, level_db(WING_MIN_DB), decode_level_db)

    async def info_query(self):  # noqa: ANN201
        reply = await self._client.query("/?", timeout=1.0)
        if reply is None:
            reply = await self._client.query("/ch/1/fdr", timeout=1.0)
        return reply
