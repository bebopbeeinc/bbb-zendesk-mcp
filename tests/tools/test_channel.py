from unittest.mock import MagicMock

from zenpy.lib.api_objects import Ticket, Via

from zendesk_mcp.tools.channel import ticket_channel


def test_channel_from_a_zenpy_via_object():
    ticket = MagicMock()
    ticket.via = Via(channel="facebook")
    assert ticket_channel(ticket) == "facebook"


def test_channel_from_a_via_dict_on_a_hand_built_ticket():
    assert ticket_channel(Ticket(id=1, via={"channel": "web", "source": {}})) == "web"


def test_channel_from_a_raw_api_ticket():
    assert ticket_channel({"id": 1, "via": {"channel": "email"}}) == "email"


def test_no_channel_is_none_never_a_value_that_will_not_serialise():
    assert ticket_channel({"id": 1}) is None
    assert ticket_channel({"id": 1, "via": None}) is None
    assert ticket_channel(MagicMock()) is None          # a test double's attribute is not a str
