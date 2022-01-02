import struct

from keycodes import Keycode
from macro.macro_action import SS_TAP_CODE, SS_DOWN_CODE, SS_UP_CODE, ActionText, ActionTap, ActionDown, ActionUp, \
    SS_QMK_PREFIX, SS_DELAY_CODE, ActionDelay
from macro.macro_action_ui import tag_to_action
from protocol.base_protocol import BaseProtocol
from protocol.constants import CMD_VIA_MACRO_GET_COUNT, CMD_VIA_MACRO_GET_BUFFER_SIZE, CMD_VIA_MACRO_GET_BUFFER, \
    CMD_VIA_MACRO_SET_BUFFER, BUFFER_FETCH_CHUNK
from unlocker import Unlocker
from util import chunks


def macro_deserialize_v1(data):
    """
    Deserialize a single macro, protocol version 1
    """

    out = []
    sequence = []
    data = bytearray(data)
    while len(data) > 0:
        if data[0] in [SS_TAP_CODE, SS_DOWN_CODE, SS_UP_CODE]:
            if len(data) < 2:
                break

            # append to previous *_CODE if it's the same type, otherwise create a new entry
            if len(sequence) > 0 and isinstance(sequence[-1], list) and sequence[-1][0] == data[0]:
                sequence[-1][1].append(data[1])
            else:
                sequence.append([data[0], [data[1]]])

            data.pop(0)
            data.pop(0)
        else:
            # append to previous string if it is a string, otherwise create a new entry
            ch = chr(data[0])
            if len(sequence) > 0 and isinstance(sequence[-1], str):
                sequence[-1] += ch
            else:
                sequence.append(ch)
            data.pop(0)
    for s in sequence:
        if isinstance(s, str):
            out.append(ActionText(s))
        else:
            # map integer values to qmk keycodes
            keycodes = []
            for code in s[1]:
                keycode = Keycode.find_outer_keycode(code)
                if keycode:
                    keycodes.append(keycode)
            cls = {SS_TAP_CODE: ActionTap, SS_DOWN_CODE: ActionDown, SS_UP_CODE: ActionUp}[s[0]]
            out.append(cls(keycodes))
    return out


def macro_deserialize_v2(data):
    """
    Deserialize a single macro, protocol version 2
    """

    out = []
    sequence = []
    data = bytearray(data)
    while len(data) > 0:
        if data[0] == SS_QMK_PREFIX:
            if len(data) < 2:
                break

            if data[1] in [SS_TAP_CODE, SS_DOWN_CODE, SS_UP_CODE]:
                if len(data) < 3:
                    break

                # append to previous *_CODE if it's the same type, otherwise create a new entry
                if len(sequence) > 0 and isinstance(sequence[-1], list) and sequence[-1][0] == data[1]:
                    sequence[-1][1].append(data[2])
                else:
                    sequence.append([data[1], [data[2]]])

                for x in range(3):
                    data.pop(0)
            elif data[1] == SS_DELAY_CODE:
                if len(data) < 4:
                    break

                # decode the delay
                delay = (data[2] - 1) + (data[3] - 1) * 255
                sequence.append([SS_DELAY_CODE, delay])

                for x in range(4):
                    data.pop(0)
            else:
                # it is clearly malformed, just skip this byte and hope for the best
                data.pop(0)
                data.pop(0)
        else:
            # append to previous string if it is a string, otherwise create a new entry
            ch = chr(data[0])
            if len(sequence) > 0 and isinstance(sequence[-1], str):
                sequence[-1] += ch
            else:
                sequence.append(ch)
            data.pop(0)
    for s in sequence:
        if isinstance(s, str):
            out.append(ActionText(s))
        else:
            args = None
            if s[0] in [SS_TAP_CODE, SS_DOWN_CODE, SS_UP_CODE]:
                # map integer values to qmk keycodes
                args = []
                for code in s[1]:
                    keycode = Keycode.find_outer_keycode(code)
                    if keycode:
                        args.append(keycode)
            elif s[0] == SS_DELAY_CODE:
                args = s[1]

            if args is not None:
                cls = {SS_TAP_CODE: ActionTap, SS_DOWN_CODE: ActionDown, SS_UP_CODE: ActionUp,
                       SS_DELAY_CODE: ActionDelay}[s[0]]
                out.append(cls(args))
    return out


class ProtocolMacro(BaseProtocol):

    def reload_macros(self):
        """ Loads macro information from the keyboard """
        data = self.usb_send(self.dev, struct.pack("B", CMD_VIA_MACRO_GET_COUNT), retries=20)
        self.macro_count = data[1]
        data = self.usb_send(self.dev, struct.pack("B", CMD_VIA_MACRO_GET_BUFFER_SIZE), retries=20)
        self.macro_memory = struct.unpack(">H", data[1:3])[0]

        self.macro = b""
        if self.macro_memory:
            # now retrieve the entire buffer, MACRO_CHUNK bytes at a time, as that is what fits into a packet
            for x in range(0, self.macro_memory, BUFFER_FETCH_CHUNK):
                sz = min(BUFFER_FETCH_CHUNK, self.macro_memory - x)
                data = self.usb_send(self.dev, struct.pack(">BHB", CMD_VIA_MACRO_GET_BUFFER, x, sz), retries=20)
                self.macro += data[4:4+sz]
                if self.macro.count(b"\x00") > self.macro_count:
                    break
            # macros are stored as NUL-separated strings, so let's clean up the buffer
            # ensuring we only get macro_count strings after we split by NUL
            macros = self.macro.split(b"\x00") + [b""] * self.macro_count
            self.macro = b"\x00".join(macros[:self.macro_count]) + b"\x00"

    def set_macro(self, data):
        if len(data) > self.macro_memory:
            raise RuntimeError("the macro is too big: got {} max {}".format(len(data), self.macro_memory))

        for x, chunk in enumerate(chunks(data, BUFFER_FETCH_CHUNK)):
            off = x * BUFFER_FETCH_CHUNK
            self.usb_send(self.dev, struct.pack(">BHB", CMD_VIA_MACRO_SET_BUFFER, off, len(chunk)) + chunk,
                          retries=20)
        self.macro = data

    def save_macro(self):
        macros = self.macros_deserialize(self.macro)
        out = []
        for macro in macros:
            out.append([act.save() for act in macro])
        return out

    def restore_macros(self, macros):
        if not isinstance(macros, list):
            return

        full_macro = []
        for macro in macros:
            actions = []
            for act in macro:
                if act[0] in tag_to_action:
                    obj = tag_to_action[act[0]]()
                    obj.restore(act)
                    actions.append(obj)
            full_macro.append(actions)
        if len(full_macro) < self.macro_count:
            full_macro += [[] for x in range(self.macro_count - len(full_macro))]
        full_macro = full_macro[:self.macro_count]
        # TODO: log a warning if macro is cutoff
        data = self.macros_serialize(full_macro)[0:self.macro_memory]
        if data != self.macro:
            Unlocker.unlock(self)
            self.set_macro(data)
            self.lock()

    def macro_serialize(self, macro):
        """
        Serialize a single macro, a macro is made out of macro actions (BasicAction)
        """
        out = b""
        for action in macro:
            out += action.serialize(self.vial_protocol)
        return out

    def macro_deserialize(self, data):
        """
        Deserialize a single macro
        """
        if self.vial_protocol >= 2:
            return macro_deserialize_v2(data)
        return macro_deserialize_v1(data)

    def macros_serialize(self, macros):
        """
        Serialize a list of macros, the list must contain all macros (macro_count)
        """
        if len(macros) != self.macro_count:
            raise RuntimeError("expected array with {} macros, got {} macros".format(self.macro_count, len(macros)))
        out = [self.macro_serialize(macro) for macro in macros]
        return b"\x00".join(out) + b"\x00"

    def macros_deserialize(self, data):
        """
        Deserialize a list of macros
        """
        macros = data.split(b"\x00")
        if len(macros) < self.macro_count:
            macros += [b""] * (self.macro_count - len(macros))
        macros = macros[:self.macro_count]
        return [self.macro_deserialize(x) for x in macros]