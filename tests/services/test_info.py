#!/usr/bin/env python
# -*- coding: utf-8 -*-


""" Unit tests for zeroconf._services.info. """

import logging
import socket
import threading
import os
import unittest
from unittest.mock import patch
from threading import Event
from typing import List

import pytest

import zeroconf as r
from zeroconf import DNSAddress, const
from zeroconf._services.info import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from .. import has_working_ipv6, _inject_response


log = logging.getLogger('zeroconf')
original_logging_level = logging.NOTSET


def setup_module():
    global original_logging_level
    original_logging_level = log.level
    log.setLevel(logging.DEBUG)


def teardown_module():
    if original_logging_level != logging.NOTSET:
        log.setLevel(original_logging_level)


class TestServiceInfo(unittest.TestCase):
    def test_get_name(self):
        """Verify the name accessor can strip the type."""
        desc = {'path': '/~paulsm/'}
        service_name = 'name._type._tcp.local.'
        service_type = '_type._tcp.local.'
        service_server = 'ash-1.local.'
        service_address = socket.inet_aton("10.0.1.2")
        info = ServiceInfo(
            service_type, service_name, 22, 0, 0, desc, service_server, addresses=[service_address]
        )
        assert info.get_name() == "name"

    def test_service_info_rejects_non_matching_updates(self):
        """Verify records with the wrong name are rejected."""

        zc = r.Zeroconf(interfaces=['127.0.0.1'])
        desc = {'path': '/~paulsm/'}
        service_name = 'name._type._tcp.local.'
        service_type = '_type._tcp.local.'
        service_server = 'ash-1.local.'
        service_address = socket.inet_aton("10.0.1.2")
        ttl = 120
        now = r.current_time_millis()
        info = ServiceInfo(
            service_type, service_name, 22, 0, 0, desc, service_server, addresses=[service_address]
        )
        # Verify backwards compatiblity with calling with None
        info.update_record(zc, now, None)
        # Matching updates
        info.update_record(
            zc,
            now,
            r.DNSText(
                service_name,
                const._TYPE_TXT,
                const._CLASS_IN | const._CLASS_UNIQUE,
                ttl,
                b'\x04ff=0\x04ci=2\x04sf=0\x0bsh=6fLM5A==',
            ),
        )
        assert info.properties[b"ci"] == b"2"
        info.update_record(
            zc,
            now,
            r.DNSService(
                service_name,
                const._TYPE_SRV,
                const._CLASS_IN | const._CLASS_UNIQUE,
                ttl,
                0,
                0,
                80,
                'ASH-2.local.',
            ),
        )
        assert info.server_key == 'ash-2.local.'
        assert info.server == 'ASH-2.local.'
        new_address = socket.inet_aton("10.0.1.3")
        info.update_record(
            zc,
            now,
            r.DNSAddress(
                'ASH-2.local.',
                const._TYPE_A,
                const._CLASS_IN | const._CLASS_UNIQUE,
                ttl,
                new_address,
            ),
        )
        assert new_address in info.addresses
        # Non-matching updates
        info.update_record(
            zc,
            now,
            r.DNSText(
                "incorrect.name.",
                const._TYPE_TXT,
                const._CLASS_IN | const._CLASS_UNIQUE,
                ttl,
                b'\x04ff=0\x04ci=3\x04sf=0\x0bsh=6fLM5A==',
            ),
        )
        assert info.properties[b"ci"] == b"2"
        info.update_record(
            zc,
            now,
            r.DNSService(
                "incorrect.name.",
                const._TYPE_SRV,
                const._CLASS_IN | const._CLASS_UNIQUE,
                ttl,
                0,
                0,
                80,
                'ASH-2.local.',
            ),
        )
        assert info.server_key == 'ash-2.local.'
        assert info.server == 'ASH-2.local.'
        new_address = socket.inet_aton("10.0.1.4")
        info.update_record(
            zc,
            now,
            r.DNSAddress(
                "incorrect.name.",
                const._TYPE_A,
                const._CLASS_IN | const._CLASS_UNIQUE,
                ttl,
                new_address,
            ),
        )
        assert new_address not in info.addresses
        zc.close()

    def test_service_info_rejects_expired_records(self):
        """Verify records that are expired are rejected."""
        zc = r.Zeroconf(interfaces=['127.0.0.1'])
        desc = {'path': '/~paulsm/'}
        service_name = 'name._type._tcp.local.'
        service_type = '_type._tcp.local.'
        service_server = 'ash-1.local.'
        service_address = socket.inet_aton("10.0.1.2")
        ttl = 120
        now = r.current_time_millis()
        info = ServiceInfo(
            service_type, service_name, 22, 0, 0, desc, service_server, addresses=[service_address]
        )
        # Matching updates
        info.update_record(
            zc,
            now,
            r.DNSText(
                service_name,
                const._TYPE_TXT,
                const._CLASS_IN | const._CLASS_UNIQUE,
                ttl,
                b'\x04ff=0\x04ci=2\x04sf=0\x0bsh=6fLM5A==',
            ),
        )
        assert info.properties[b"ci"] == b"2"
        # Expired record
        expired_record = r.DNSText(
            service_name,
            const._TYPE_TXT,
            const._CLASS_IN | const._CLASS_UNIQUE,
            ttl,
            b'\x04ff=0\x04ci=3\x04sf=0\x0bsh=6fLM5A==',
        )
        expired_record.set_created_ttl(1000, 1)
        info.update_record(zc, now, expired_record)
        assert info.properties[b"ci"] == b"2"
        zc.close()

    @unittest.skipIf(not has_working_ipv6(), 'Requires IPv6')
    @unittest.skipIf(os.environ.get('SKIP_IPV6'), 'IPv6 tests disabled')
    def test_get_info_partial(self):

        zc = r.Zeroconf(interfaces=['127.0.0.1'])

        service_name = 'name._type._tcp.local.'
        service_type = '_type._tcp.local.'
        service_server = 'ash-1.local.'
        service_text = b'path=/~matt1/'
        service_address = '10.0.1.2'
        service_address_v6_ll = 'fe80::52e:c2f2:bc5f:e9c6'
        service_scope_id = 12

        service_info = None
        send_event = Event()
        service_info_event = Event()

        last_sent = None  # type: Optional[r.DNSOutgoing]

        def send(out, addr=const._MDNS_ADDR, port=const._MDNS_PORT, v6_flow_scope=()):
            """Sends an outgoing packet."""
            nonlocal last_sent

            last_sent = out
            send_event.set()

        # patch the zeroconf send
        with patch.object(zc, "async_send", send):

            def mock_incoming_msg(records) -> r.DNSIncoming:

                generated = r.DNSOutgoing(const._FLAGS_QR_RESPONSE)

                for record in records:
                    generated.add_answer_at_time(record, 0)

                return r.DNSIncoming(generated.packets()[0])

            def get_service_info_helper(zc, type, name):
                nonlocal service_info
                service_info = zc.get_service_info(type, name)
                service_info_event.set()

            try:
                ttl = 120
                helper_thread = threading.Thread(
                    target=get_service_info_helper, args=(zc, service_type, service_name)
                )
                helper_thread.start()
                wait_time = 1

                # Expext query for SRV, TXT, A, AAAA
                send_event.wait(wait_time)
                assert last_sent is not None
                assert len(last_sent.questions) == 4
                assert r.DNSQuestion(service_name, const._TYPE_SRV, const._CLASS_IN) in last_sent.questions
                assert r.DNSQuestion(service_name, const._TYPE_TXT, const._CLASS_IN) in last_sent.questions
                assert r.DNSQuestion(service_name, const._TYPE_A, const._CLASS_IN) in last_sent.questions
                assert r.DNSQuestion(service_name, const._TYPE_AAAA, const._CLASS_IN) in last_sent.questions
                assert service_info is None

                # Expext query for SRV, A, AAAA
                last_sent = None
                send_event.clear()
                _inject_response(
                    zc,
                    mock_incoming_msg(
                        [
                            r.DNSText(
                                service_name,
                                const._TYPE_TXT,
                                const._CLASS_IN | const._CLASS_UNIQUE,
                                ttl,
                                service_text,
                            )
                        ]
                    ),
                )
                send_event.wait(wait_time)
                assert last_sent is not None
                assert len(last_sent.questions) == 3
                assert r.DNSQuestion(service_name, const._TYPE_SRV, const._CLASS_IN) in last_sent.questions
                assert r.DNSQuestion(service_name, const._TYPE_A, const._CLASS_IN) in last_sent.questions
                assert r.DNSQuestion(service_name, const._TYPE_AAAA, const._CLASS_IN) in last_sent.questions
                assert service_info is None

                # Expext query for A, AAAA
                last_sent = None
                send_event.clear()
                _inject_response(
                    zc,
                    mock_incoming_msg(
                        [
                            r.DNSService(
                                service_name,
                                const._TYPE_SRV,
                                const._CLASS_IN | const._CLASS_UNIQUE,
                                ttl,
                                0,
                                0,
                                80,
                                service_server,
                            )
                        ]
                    ),
                )
                send_event.wait(wait_time)
                assert last_sent is not None
                assert len(last_sent.questions) == 2
                assert r.DNSQuestion(service_server, const._TYPE_A, const._CLASS_IN) in last_sent.questions
                assert r.DNSQuestion(service_server, const._TYPE_AAAA, const._CLASS_IN) in last_sent.questions
                last_sent = None
                assert service_info is None

                # Expext no further queries
                last_sent = None
                send_event.clear()
                _inject_response(
                    zc,
                    mock_incoming_msg(
                        [
                            r.DNSAddress(
                                service_server,
                                const._TYPE_A,
                                const._CLASS_IN | const._CLASS_UNIQUE,
                                ttl,
                                socket.inet_pton(socket.AF_INET, service_address),
                            ),
                            r.DNSAddress(
                                service_server,
                                const._TYPE_AAAA,
                                const._CLASS_IN | const._CLASS_UNIQUE,
                                ttl,
                                socket.inet_pton(socket.AF_INET6, service_address_v6_ll),
                                scope_id=service_scope_id,
                            ),
                        ]
                    ),
                )
                send_event.wait(wait_time)
                assert last_sent is None
                assert service_info is not None

            finally:
                helper_thread.join()
                zc.remove_all_service_listeners()
                zc.close()

    def test_get_info_single(self):

        zc = r.Zeroconf(interfaces=['127.0.0.1'])

        service_name = 'name._type._tcp.local.'
        service_type = '_type._tcp.local.'
        service_server = 'ash-1.local.'
        service_text = b'path=/~matt1/'
        service_address = '10.0.1.2'

        service_info = None
        send_event = Event()
        service_info_event = Event()

        last_sent = None  # type: Optional[r.DNSOutgoing]

        def send(out, addr=const._MDNS_ADDR, port=const._MDNS_PORT, v6_flow_scope=()):
            """Sends an outgoing packet."""
            nonlocal last_sent

            last_sent = out
            send_event.set()

        # patch the zeroconf send
        with patch.object(zc, "async_send", send):

            def mock_incoming_msg(records) -> r.DNSIncoming:

                generated = r.DNSOutgoing(const._FLAGS_QR_RESPONSE)

                for record in records:
                    generated.add_answer_at_time(record, 0)

                return r.DNSIncoming(generated.packets()[0])

            def get_service_info_helper(zc, type, name):
                nonlocal service_info
                service_info = zc.get_service_info(type, name)
                service_info_event.set()

            try:
                ttl = 120
                helper_thread = threading.Thread(
                    target=get_service_info_helper, args=(zc, service_type, service_name)
                )
                helper_thread.start()
                wait_time = 1

                # Expext query for SRV, TXT, A, AAAA
                send_event.wait(wait_time)
                assert last_sent is not None
                assert len(last_sent.questions) == 4
                assert r.DNSQuestion(service_name, const._TYPE_SRV, const._CLASS_IN) in last_sent.questions
                assert r.DNSQuestion(service_name, const._TYPE_TXT, const._CLASS_IN) in last_sent.questions
                assert r.DNSQuestion(service_name, const._TYPE_A, const._CLASS_IN) in last_sent.questions
                assert r.DNSQuestion(service_name, const._TYPE_AAAA, const._CLASS_IN) in last_sent.questions
                assert service_info is None

                # Expext no further queries
                last_sent = None
                send_event.clear()
                _inject_response(
                    zc,
                    mock_incoming_msg(
                        [
                            r.DNSText(
                                service_name,
                                const._TYPE_TXT,
                                const._CLASS_IN | const._CLASS_UNIQUE,
                                ttl,
                                service_text,
                            ),
                            r.DNSService(
                                service_name,
                                const._TYPE_SRV,
                                const._CLASS_IN | const._CLASS_UNIQUE,
                                ttl,
                                0,
                                0,
                                80,
                                service_server,
                            ),
                            r.DNSAddress(
                                service_server,
                                const._TYPE_A,
                                const._CLASS_IN | const._CLASS_UNIQUE,
                                ttl,
                                socket.inet_pton(socket.AF_INET, service_address),
                            ),
                        ]
                    ),
                )
                send_event.wait(wait_time)
                assert last_sent is None
                assert service_info is not None

            finally:
                helper_thread.join()
                zc.remove_all_service_listeners()
                zc.close()

    def test_service_info_duplicate_properties_txt_records(self):
        """Verify the first property is always used when there are duplicates in a txt record."""

        zc = r.Zeroconf(interfaces=['127.0.0.1'])
        desc = {'path': '/~paulsm/'}
        service_name = 'name._type._tcp.local.'
        service_type = '_type._tcp.local.'
        service_server = 'ash-1.local.'
        service_address = socket.inet_aton("10.0.1.2")
        ttl = 120
        now = r.current_time_millis()
        info = ServiceInfo(
            service_type, service_name, 22, 0, 0, desc, service_server, addresses=[service_address]
        )
        info.async_update_records(
            zc,
            now,
            [
                r.RecordUpdate(
                    r.DNSText(
                        service_name,
                        const._TYPE_TXT,
                        const._CLASS_IN | const._CLASS_UNIQUE,
                        ttl,
                        b'\x04ff=0\x04ci=2\x04sf=0\x0bsh=6fLM5A==\x04dd=0\x04jl=2\x04qq=0\x0brr=6fLM5A==\x04ci=3',
                    ),
                    None,
                )
            ],
        )
        assert info.properties[b"dd"] == b"0"
        assert info.properties[b"jl"] == b"2"
        assert info.properties[b"ci"] == b"2"
        zc.close()


def test_multiple_addresses():
    type_ = "_http._tcp.local."
    registration_name = "xxxyyy.%s" % type_
    desc = {'path': '/~paulsm/'}
    address_parsed = "10.0.1.2"
    address = socket.inet_aton(address_parsed)

    # New kwarg way
    info = ServiceInfo(type_, registration_name, 80, 0, 0, desc, "ash-2.local.", addresses=[address, address])

    assert info.addresses == [address, address]
    assert info.parsed_addresses() == [address_parsed, address_parsed]
    assert info.parsed_scoped_addresses() == [address_parsed, address_parsed]

    info = ServiceInfo(
        type_,
        registration_name,
        80,
        0,
        0,
        desc,
        "ash-2.local.",
        parsed_addresses=[address_parsed, address_parsed],
    )
    assert info.addresses == [address, address]
    assert info.parsed_addresses() == [address_parsed, address_parsed]
    assert info.parsed_scoped_addresses() == [address_parsed, address_parsed]

    if has_working_ipv6() and not os.environ.get('SKIP_IPV6'):
        address_v6_parsed = "2001:db8::1"
        address_v6 = socket.inet_pton(socket.AF_INET6, address_v6_parsed)
        address_v6_ll_parsed = "fe80::52e:c2f2:bc5f:e9c6"
        address_v6_ll_scoped_parsed = "fe80::52e:c2f2:bc5f:e9c6%12"
        address_v6_ll = socket.inet_pton(socket.AF_INET6, address_v6_ll_parsed)
        interface_index = 12
        infos = [
            ServiceInfo(
                type_,
                registration_name,
                80,
                0,
                0,
                desc,
                "ash-2.local.",
                addresses=[address, address_v6, address_v6_ll],
                interface_index=interface_index,
            ),
            ServiceInfo(
                type_,
                registration_name,
                80,
                0,
                0,
                desc,
                "ash-2.local.",
                parsed_addresses=[address_parsed, address_v6_parsed, address_v6_ll_parsed],
                interface_index=interface_index,
            ),
        ]
        for info in infos:
            assert info.addresses == [address]
            assert info.addresses_by_version(r.IPVersion.All) == [address, address_v6, address_v6_ll]
            assert info.addresses_by_version(r.IPVersion.V4Only) == [address]
            assert info.addresses_by_version(r.IPVersion.V6Only) == [address_v6, address_v6_ll]
            assert info.parsed_addresses() == [address_parsed, address_v6_parsed, address_v6_ll_parsed]
            assert info.parsed_addresses(r.IPVersion.V4Only) == [address_parsed]
            assert info.parsed_addresses(r.IPVersion.V6Only) == [address_v6_parsed, address_v6_ll_parsed]
            assert info.parsed_scoped_addresses() == [
                address_v6_ll_scoped_parsed,
                address_parsed,
                address_v6_parsed,
            ]
            assert info.parsed_scoped_addresses(r.IPVersion.V4Only) == [address_parsed]
            assert info.parsed_scoped_addresses(r.IPVersion.V6Only) == [
                address_v6_ll_scoped_parsed,
                address_v6_parsed,
            ]


# This test uses asyncio because it needs to access the cache directly
# which is not threadsafe
@pytest.mark.asyncio
async def test_multiple_a_addresses():
    type_ = "_http._tcp.local."
    registration_name = "multiarec.%s" % type_
    desc = {'path': '/~paulsm/'}
    aiozc = AsyncZeroconf(interfaces=['127.0.0.1'])
    cache = aiozc.zeroconf.cache
    host = "multahost.local."
    record1 = r.DNSAddress(host, const._TYPE_A, const._CLASS_IN, 1000, b'a')
    record2 = r.DNSAddress(host, const._TYPE_A, const._CLASS_IN, 1000, b'b')
    cache.async_add_records([record1, record2])

    # New kwarg way
    info = ServiceInfo(type_, registration_name, 80, 0, 0, desc, host)
    info.load_from_cache(aiozc.zeroconf)
    assert set(info.addresses) == set([b'a', b'b'])
    await aiozc.async_close()


@unittest.skipIf(not has_working_ipv6(), 'Requires IPv6')
@unittest.skipIf(os.environ.get('SKIP_IPV6'), 'IPv6 tests disabled')
def test_filter_address_by_type_from_service_info():
    """Verify dns_addresses can filter by ipversion."""
    desc = {'path': '/~paulsm/'}
    type_ = "_homeassistant._tcp.local."
    name = "MyTestHome"
    registration_name = "%s.%s" % (name, type_)
    ipv4 = socket.inet_aton("10.0.1.2")
    ipv6 = socket.inet_pton(socket.AF_INET6, "2001:db8::1")
    info = ServiceInfo(type_, registration_name, 80, 0, 0, desc, "ash-2.local.", addresses=[ipv4, ipv6])

    def dns_addresses_to_addresses(dns_address: List[DNSAddress]):
        return [address.address for address in dns_address]

    assert dns_addresses_to_addresses(info.dns_addresses()) == [ipv4, ipv6]
    assert dns_addresses_to_addresses(info.dns_addresses(version=r.IPVersion.All)) == [ipv4, ipv6]
    assert dns_addresses_to_addresses(info.dns_addresses(version=r.IPVersion.V4Only)) == [ipv4]
    assert dns_addresses_to_addresses(info.dns_addresses(version=r.IPVersion.V6Only)) == [ipv6]


def test_changing_name_updates_serviceinfo_key():
    """Verify a name change will adjust the underlying key value."""
    type_ = "_homeassistant._tcp.local."
    name = "MyTestHome"
    info_service = ServiceInfo(
        type_,
        '%s.%s' % (name, type_),
        80,
        0,
        0,
        {'path': '/~paulsm/'},
        "ash-2.local.",
        addresses=[socket.inet_aton("10.0.1.2")],
    )
    assert info_service.key == "mytesthome._homeassistant._tcp.local."
    info_service.name = "YourTestHome._homeassistant._tcp.local."
    assert info_service.key == "yourtesthome._homeassistant._tcp.local."


def test_serviceinfo_address_updates():
    """Verify adding/removing/setting addresses on ServiceInfo."""
    type_ = "_homeassistant._tcp.local."
    name = "MyTestHome"

    # Verify addresses and parsed_addresses are mutually exclusive
    with pytest.raises(TypeError):
        info_service = ServiceInfo(
            type_,
            '%s.%s' % (name, type_),
            80,
            0,
            0,
            {'path': '/~paulsm/'},
            "ash-2.local.",
            addresses=[socket.inet_aton("10.0.1.2")],
            parsed_addresses=["10.0.1.2"],
        )

    info_service = ServiceInfo(
        type_,
        '%s.%s' % (name, type_),
        80,
        0,
        0,
        {'path': '/~paulsm/'},
        "ash-2.local.",
        addresses=[socket.inet_aton("10.0.1.2")],
    )
    info_service.addresses = [socket.inet_aton("10.0.1.3")]
    assert info_service.addresses == [socket.inet_aton("10.0.1.3")]


def test_serviceinfo_accepts_bytes_or_string_dict():
    """Verify a bytes or string dict can be passed to ServiceInfo."""
    type_ = "_homeassistant._tcp.local."
    name = "MyTestHome"
    addresses = [socket.inet_aton("10.0.1.2")]
    server_name = "ash-2.local."
    info_service = ServiceInfo(
        type_, '%s.%s' % (name, type_), 80, 0, 0, {b'path': b'/~paulsm/'}, server_name, addresses=addresses
    )
    assert info_service.dns_text().text == b'\x0epath=/~paulsm/'
    info_service = ServiceInfo(
        type_,
        '%s.%s' % (name, type_),
        80,
        0,
        0,
        {'path': '/~paulsm/'},
        server_name,
        addresses=addresses,
    )
    assert info_service.dns_text().text == b'\x0epath=/~paulsm/'
    info_service = ServiceInfo(
        type_,
        '%s.%s' % (name, type_),
        80,
        0,
        0,
        {b'path': '/~paulsm/'},
        server_name,
        addresses=addresses,
    )
    assert info_service.dns_text().text == b'\x0epath=/~paulsm/'
    info_service = ServiceInfo(
        type_,
        '%s.%s' % (name, type_),
        80,
        0,
        0,
        {'path': b'/~paulsm/'},
        server_name,
        addresses=addresses,
    )
    assert info_service.dns_text().text == b'\x0epath=/~paulsm/'


def test_asking_qu_questions():
    """Verify explictly asking QU questions."""
    type_ = "_quservice._tcp.local."
    zeroconf = r.Zeroconf(interfaces=['127.0.0.1'])

    # we are going to patch the zeroconf send to check query transmission
    old_send = zeroconf.async_send

    first_outgoing = None

    def send(out, addr=const._MDNS_ADDR, port=const._MDNS_PORT):
        """Sends an outgoing packet."""
        nonlocal first_outgoing
        if first_outgoing is None:
            first_outgoing = out
        old_send(out, addr=addr, port=port)

    # patch the zeroconf send
    with patch.object(zeroconf, "async_send", send):
        zeroconf.get_service_info(f"name.{type_}", type_, 500, question_type=r.DNSQuestionType.QU)
        assert first_outgoing.questions[0].unicast == True
        zeroconf.close()


def test_asking_qm_questions():
    """Verify explictly asking QM questions."""
    type_ = "_quservice._tcp.local."
    zeroconf = r.Zeroconf(interfaces=['127.0.0.1'])

    # we are going to patch the zeroconf send to check query transmission
    old_send = zeroconf.async_send

    first_outgoing = None

    def send(out, addr=const._MDNS_ADDR, port=const._MDNS_PORT):
        """Sends an outgoing packet."""
        nonlocal first_outgoing
        if first_outgoing is None:
            first_outgoing = out
        old_send(out, addr=addr, port=port)

    # patch the zeroconf send
    with patch.object(zeroconf, "async_send", send):
        zeroconf.get_service_info(f"name.{type_}", type_, 500, question_type=r.DNSQuestionType.QM)
        assert first_outgoing.questions[0].unicast == False
        zeroconf.close()


def test_request_timeout():
    """Test that the timeout does not throw an exception and finishes close to the actual timeout."""
    zeroconf = r.Zeroconf(interfaces=['127.0.0.1'])
    start_time = r.current_time_millis()
    assert zeroconf.get_service_info("_notfound.local.", "notthere._notfound.local.") is None
    end_time = r.current_time_millis()
    zeroconf.close()
    # 3000ms for the default timeout
    # 1000ms for loaded systems + schedule overhead
    assert (end_time - start_time) < 3000 + 1000


@pytest.mark.asyncio
async def test_we_try_four_times_with_random_delay():
    """Verify we try four times even with the random delay."""
    type_ = "_typethatisnothere._tcp.local."
    aiozc = AsyncZeroconf(interfaces=['127.0.0.1'])

    # we are going to patch the zeroconf send to check query transmission
    request_count = 0

    def async_send(out, addr=const._MDNS_ADDR, port=const._MDNS_PORT):
        """Sends an outgoing packet."""
        nonlocal request_count
        request_count += 1

    # patch the zeroconf send
    with patch.object(aiozc.zeroconf, "async_send", async_send):
        await aiozc.async_get_service_info(f"willnotbefound.{type_}", type_)

    await aiozc.async_close()

    assert request_count == 4
