# -*- coding: utf-8 -*-
"""c2s protocol handlers."""
"""
  Kontalk XMPP server
  Copyright (C) 2014 Kontalk Devteam <devteam@kontalk.org>

 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""


import time
import base64
import traceback

from twisted.words.protocols.jabber import xmlstream, jid
from twisted.words.protocols.jabber.xmlstream import XMPPHandler
from twisted.words.xish import domish

from wokkel import component

from kontalk.xmppserver import log, util, xmlstream2


class InitialPresenceHandler(XMPPHandler):
    """
    Handle presence stanzas and client disconnection.
    @type parent: L{C2SManager}
    """

    def connectionInitialized(self):
        self.xmlstream.addObserver("/presence[not(@type)][@to='%s']" % (self.xmlstream.thisEntity.full(), ), self.presence)

    def send_presence(self, to):
        """
        Sends all local presence data (available and unavailable) to the given
        entity.
        """

        def _db(presence, to):
            from copy import deepcopy
            log.debug("presence: %r" % (presence, ))
            if type(presence) == list and len(presence) > 0:

                for user in presence:
                    response_from = util.userid_to_jid(user['userid'], self.parent.xmlstream.thisEntity.host).full()

                    num_avail = 0
                    try:
                        streams = self.parent.sfactory.streams[user['userid']]
                        for x in streams.itervalues():
                            presence = x._presence
                            if presence and not presence.hasAttribute('type'):
                                response = domish.Element((None, 'presence'))
                                response['to'] = to
                                response['from'] = presence['from']

                                # copy stuff
                                for child in ('status', 'show', 'priority'):
                                    e = getattr(presence, child)
                                    if e:
                                        response.addChild(deepcopy(e))

                                self.send(response)

                                num_avail += 1
                    except KeyError:
                        pass

                    # no available resources - send unavailable presence
                    if not num_avail:
                        response = domish.Element((None, 'presence'))
                        response['to'] = to
                        response['from'] = response_from

                        if user['status'] is not None:
                            response.addElement((None, 'status'), content=user['status'])
                        if user['show'] is not None:
                            response.addElement((None, 'show'), content=user['show'])

                        response['type'] = 'unavailable'
                        delay = domish.Element(('urn:xmpp:delay', 'delay'))
                        delay['stamp'] = user['timestamp'].strftime(xmlstream2.XMPP_STAMP_FORMAT)
                        response.addChild(delay)

                        self.send(response)

                    if self.parent.logTraffic:
                        log.debug("presence sent: %s" % (response.toXml().encode('utf-8'), ))
                    else:
                        log.debug("presence sent: %s" % (response['from'], ))

                    # send vcard
                    iq_vcard = domish.Element((None, 'iq'))
                    iq_vcard['type'] = 'set'
                    iq_vcard['from'] = response_from
                    iq_vcard['to'] = to

                    # add vcard
                    vcard = iq_vcard.addElement((xmlstream2.NS_XMPP_VCARD4, 'vcard'))
                    if user['fingerprint']:
                        pub_key = self.parent.keyring.get_key(user['userid'], user['fingerprint'])
                        if pub_key:
                            vcard_key = vcard.addElement((None, 'key'))
                            vcard_data = vcard_key.addElement((None, 'uri'))
                            vcard_data.addContent("data:application/pgp-keys;base64," + base64.b64encode(pub_key))

                    self.send(iq_vcard)
                    if self.parent.logTraffic:
                        log.debug("vCard sent: %s" % (iq_vcard.toXml().encode('utf-8'), ))
                    else:
                        log.debug("vCard sent: %s" % (iq_vcard['from'], ))

        d = self.parent.presencedb.get_all()
        d.addCallback(_db, to)

    def presence(self, stanza):
        """
        This initial presence is from a broadcast sent by external entities
        (e.g. not the sm); sm wouldn't see it because it has no observer.
        Here we are sending offline messages directly to the connected user.
        """

        log.debug("initial presence from router by %s" % (stanza['from'], ))

        try:
            # receiving initial presence from remote c2s, send all presence data
            unused, host = util.jid_component(stanza['from'], util.COMPONENT_C2S)

            if host != self.parent.servername and host in self.parent.keyring.hostlist():
                log.debug("remote c2s appeared, sending all local presence and vCards to %s" % (stanza['from'], ))
                self.send_presence(stanza['from'])

        except:
            pass

        sender = jid.JID(stanza['from'])

        # check for external conflict
        self.parent.sfactory.check_conflict(sender)

        if sender.user:
            try:
                unused, host = util.jid_component(sender.host, util.COMPONENT_C2S)

                # initial presence from a client connected to another server, clear it from our presence table
                if host != self.parent.servername and host in self.parent.keyring.hostlist():
                    log.debug("deleting %s from presence table" % (sender.user, ))
                    self.parent.presencedb.delete(sender.user)

            except:
                pass

        # initial presence - deliver offline storage
        def output(data, user):
            log.debug("data: %r" % (data, ))
            to = user.full()

            for msg in data:
                log.debug("msg[%s]=%s" % (msg['id'], msg['stanza'].toXml().encode('utf-8'), ))
                try:
                    """
                    Mark the stanza with our server name, so we'll receive a
                    copy of the receipt
                    """
                    if msg['stanza'].request:
                        msg['stanza'].request['from'] = self.xmlstream.thisEntity.full()
                    elif msg['stanza'].received:
                        msg['stanza'].received['from'] = self.xmlstream.thisEntity.full()

                    # mark delayed delivery
                    if 'timestamp' in msg:
                        delay = msg['stanza'].addElement((xmlstream2.NS_XMPP_DELAY, 'delay'))
                        delay['stamp'] = msg['timestamp'].strftime(xmlstream2.XMPP_STAMP_FORMAT)

                    msg['to'] = to
                    self.send(msg['stanza'])
                    """
                    If a receipt is requested, we won't delete the message from
                    storage now; we must be sure client has received it.
                    Otherwise just delete the message immediately.
                    """
                    if not xmlstream2.extract_receipt(msg['stanza'], 'request') and \
                            not xmlstream2.extract_receipt(stanza, 'received'):
                        self.parent.message_offline_delete(msg['id'], msg['stanza'].name)
                except:
                    log.debug("offline message delivery failed (%s)" % (msg['id'], ))
                    traceback.print_exc()

        d = self.parent.stanzadb.get_by_recipient(sender)
        d.addCallback(output, sender)


class PresenceProbeHandler(XMPPHandler):
    """Handles presence stanza with type 'probe'."""

    def __init__(self):
        XMPPHandler.__init__(self)

    def connectionInitialized(self):
        self.xmlstream.addObserver("/presence[@type='probe']", self.probe, 100)

    def probe(self, stanza):
        """Handle presence probes from router."""
        #log.debug("local presence probe: %s" % (stanza.toXml(), ))
        stanza.consumed = True

        def _db(presence, stanza):
            log.debug("presence: %r" % (presence, ))
            if type(presence) == list and len(presence) > 0:
                chain = domish.Element((xmlstream2.NS_XMPP_STANZA_GROUP, 'group'))
                chain['id'] = stanza['id']
                chain['count'] = str(len(presence))

                for user in presence:
                    response = xmlstream.toResponse(stanza)
                    response['id'] = util.rand_str(8, util.CHARSBOX_AZN_LOWERCASE)
                    response_from = util.userid_to_jid(user['userid'], self.xmlstream.thisEntity.host)
                    response['from'] = response_from.full()

                    if user['status'] is not None:
                        response.addElement((None, 'status'), content=user['status'])
                    if user['show'] is not None:
                        response.addElement((None, 'show'), content=user['show'])

                    if not self.parent.sfactory.client_connected(response_from):
                        response['type'] = 'unavailable'
                        delay = domish.Element(('urn:xmpp:delay', 'delay'))
                        delay['stamp'] = user['timestamp'].strftime(xmlstream2.XMPP_STAMP_FORMAT)
                        response.addChild(delay)

                    response.addChild(chain)

                    self.send(response)

                    if self.parent.logTraffic:
                        log.debug("probe result sent: %s" % (response.toXml().encode('utf-8'), ))
                    else:
                        log.debug("probe result sent: %s" % (response['from'], ))

            elif presence is not None and type(presence) != list:
                chain = domish.Element((xmlstream2.NS_XMPP_STANZA_GROUP, 'group'))
                chain['id'] = stanza['id']
                chain['count'] = '1'

                response = xmlstream.toResponse(stanza)

                if presence['status'] is not None:
                    response.addElement((None, 'status'), content=presence['status'])
                if presence['show'] is not None:
                    response.addElement((None, 'show'), content=presence['show'])

                response_from = util.userid_to_jid(presence['userid'], self.parent.servername)
                if not self.parent.sfactory.client_connected(response_from):
                    response['type'] = 'unavailable'
                    delay = domish.Element(('urn:xmpp:delay', 'delay'))
                    delay['stamp'] = presence['timestamp'].strftime(xmlstream2.XMPP_STAMP_FORMAT)
                    response.addChild(delay)

                response.addChild(chain)
                self.send(response)

                if self.parent.logTraffic:
                    log.debug("probe result sent: %s" % (response.toXml().encode('utf-8'), ))
                else:
                    log.debug("probe result sent: %s" % (response['from'], ))
            else:
                log.debug("probe: user not found")
                # TODO return error?
                response = xmlstream.toResponse(stanza, 'error')

                chain = domish.Element((xmlstream2.NS_XMPP_STANZA_GROUP, 'group'))
                chain['id'] = stanza['id']
                chain['count'] = '1'
                response.addChild(chain)

                self.send(response)

        userid = util.jid_user(stanza['to'])
        d = self.parent.presencedb.get(userid)
        d.addCallback(_db, stanza)


class LastActivityHandler(XMPPHandler):
    """
    XEP-0012: Last activity
    http://xmpp.org/extensions/xep-0012.html
    TODO this needs serious fixing
    """
    def __init__(self):
        XMPPHandler.__init__(self)

    def connectionInitialized(self):
        self.xmlstream.addObserver("/iq[@type='get']/query[@xmlns='%s']" % (xmlstream2.NS_IQ_LAST, ), self.last_activity, 100)

    def last_activity(self, stanza):
        log.debug("local last activity request: %s" % (stanza.toXml(), ))
        stanza.consumed = True

        def _db(presence, stanza):
            log.debug("iq/last: presence=%r" % (presence, ))
            if type(presence) == list and len(presence) > 0:
                user = presence[0]

                response = xmlstream.toResponse(stanza, 'result')
                response_from = util.userid_to_jid(user['userid'], self.xmlstream.thisEntity.host)
                response['from'] = response_from.userhost()

                query = response.addElement((xmlstream2.NS_IQ_LAST, 'query'))
                if self.parent.sfactory.client_connected(response_from):
                    query['seconds'] = '0'
                else:
                    latest = None
                    for user in presence:
                        if latest is None or latest['timestamp'] > user['timestamp']:
                            latest = user
                    # TODO timediff from latest
                    #log.debug("max timestamp: %r" % (max, ))
                    query['seconds'] = '123456'

                self.send(response)
                log.debug("iq/last result sent: %s" % (response.toXml().encode('utf-8'), ))

            else:
                # TODO return error?
                log.debug("iq/last: user not found")

        userid = util.jid_user(stanza['to'])
        d = self.parent.presencedb.get(userid)
        d.addCallback(_db, stanza)


class MessageHandler(XMPPHandler):
    """Message stanzas handler."""

    def connectionInitialized(self):
        self.xmlstream.addObserver("/message", self.dispatch)
        self.xmlstream.addObserver("/message/ack[@xmlns='%s']" % (xmlstream2.NS_XMPP_SERVER_RECEIPTS), self.ack, 100)
        self.xmlstream.addObserver("/message[@type='error']/error/network-server-timeout", self.network_timeout, 100)

    def features(self):
        return tuple()

    def ack(self, stanza):
        stanza.consumed = True
        msgId = stanza['id']
        if msgId:
            try:
                if stanza['to'] == self.xmlstream.thisEntity.full():
                    self.parent.message_offline_delete(msgId, stanza.name)
            except:
                traceback.print_exc()

    def network_timeout(self, stanza):
        """
        Handles errors from the net component (e.g. kontalk server not responding).
        """
        stanza.consumed = True
        util.resetNamespace(stanza, component.NS_COMPONENT_ACCEPT)
        message = stanza.original.firstChildElement()
        self.parent.not_found(message)

        # send ack only for chat messages
        if message.getAttribute('type') == 'chat':
            self.parent.send_ack(message, 'sent')

    def dispatch(self, stanza):
        """Incoming message from router."""
        if not stanza.consumed:
            if self.parent.logTraffic:
                log.debug("incoming message: %s" % (stanza.toXml().encode('utf-8')))

            stanza.consumed = True

            util.resetNamespace(stanza, component.NS_COMPONENT_ACCEPT)
            self.parent.process_message(stanza)
