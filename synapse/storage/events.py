# -*- coding: utf-8 -*-
# Copyright 2014, 2015 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from _base import SQLBaseStore, _RollbackButIsFineException

from twisted.internet import defer

from synapse.util.logutils import log_function
from synapse.api.constants import EventTypes
from synapse.crypto.event_signing import compute_event_reference_hash

from syutil.base64util import decode_base64
from syutil.jsonutil import encode_canonical_json

import logging

logger = logging.getLogger(__name__)


class EventsStore(SQLBaseStore):
    @defer.inlineCallbacks
    @log_function
    def persist_event(self, event, context, backfilled=False,
                      is_new_state=True, current_state=None):
        stream_ordering = None
        if backfilled:
            if not self.min_token_deferred.called:
                yield self.min_token_deferred
            self.min_token -= 1
            stream_ordering = self.min_token

        try:
            yield self.runInteraction(
                "persist_event",
                self._persist_event_txn,
                event=event,
                context=context,
                backfilled=backfilled,
                stream_ordering=stream_ordering,
                is_new_state=is_new_state,
                current_state=current_state,
            )
        except _RollbackButIsFineException:
            pass

    @defer.inlineCallbacks
    def get_event(self, event_id, check_redacted=True,
                  get_prev_content=False, allow_rejected=False,
                  allow_none=False):
        """Get an event from the database by event_id.

        Args:
            event_id (str): The event_id of the event to fetch
            check_redacted (bool): If True, check if event has been redacted
                and redact it.
            get_prev_content (bool): If True and event is a state event,
                include the previous states content in the unsigned field.
            allow_rejected (bool): If True return rejected events.
            allow_none (bool): If True, return None if no event found, if
                False throw an exception.

        Returns:
            Deferred : A FrozenEvent.
        """
        event = yield self.runInteraction(
            "get_event", self._get_event_txn,
            event_id,
            check_redacted=check_redacted,
            get_prev_content=get_prev_content,
            allow_rejected=allow_rejected,
        )

        if not event and not allow_none:
            raise RuntimeError("Could not find event %s" % (event_id,))

        defer.returnValue(event)

    @log_function
    def _persist_event_txn(self, txn, event, context, backfilled,
                           stream_ordering=None, is_new_state=True,
                           current_state=None):

        # Remove the any existing cache entries for the event_id
        txn.call_after(self._invalidate_get_event_cache, event.event_id)

        if stream_ordering is None:
            with self._stream_id_gen.get_next_txn(txn) as stream_ordering:
                return self._persist_event_txn(
                    txn, event, context, backfilled,
                    stream_ordering=stream_ordering,
                    is_new_state=is_new_state,
                    current_state=current_state,
                )

        # We purposefully do this first since if we include a `current_state`
        # key, we *want* to update the `current_state_events` table
        if current_state:
            self._simple_delete_txn(
                txn,
                table="current_state_events",
                keyvalues={"room_id": event.room_id},
            )

            for s in current_state:
                if s.type == EventTypes.Member:
                    txn.call_after(
                        self.get_rooms_for_user.invalidate, s.state_key
                    )
                    txn.call_after(
                        self.get_joined_hosts_for_room.invalidate, s.room_id
                    )
                self._simple_insert_txn(
                    txn,
                    "current_state_events",
                    {
                        "event_id": s.event_id,
                        "room_id": s.room_id,
                        "type": s.type,
                        "state_key": s.state_key,
                    }
                )

        outlier = event.internal_metadata.is_outlier()

        if not outlier:
            self._store_state_groups_txn(txn, event, context)

            self._update_min_depth_for_room_txn(
                txn,
                event.room_id,
                event.depth
            )

        have_persisted = self._simple_select_one_onecol_txn(
            txn,
            table="event_json",
            keyvalues={"event_id": event.event_id},
            retcol="event_id",
            allow_none=True,
        )

        metadata_json = encode_canonical_json(
            event.internal_metadata.get_dict()
        ).decode("UTF-8")

        # If we have already persisted this event, we don't need to do any
        # more processing.
        # The processing above must be done on every call to persist event,
        # since they might not have happened on previous calls. For example,
        # if we are persisting an event that we had persisted as an outlier,
        # but is no longer one.
        if have_persisted:
            if not outlier:
                sql = (
                    "UPDATE event_json SET internal_metadata = ?"
                    " WHERE event_id = ?"
                )
                txn.execute(
                    sql,
                    (metadata_json, event.event_id,)
                )

                sql = (
                    "UPDATE events SET outlier = ?"
                    " WHERE event_id = ?"
                )
                txn.execute(
                    sql,
                    (False, event.event_id,)
                )
            return

        self._handle_prev_events(
            txn,
            outlier=outlier,
            event_id=event.event_id,
            prev_events=event.prev_events,
            room_id=event.room_id,
        )

        if event.type == EventTypes.Member:
            self._store_room_member_txn(txn, event)
        elif event.type == EventTypes.Name:
            self._store_room_name_txn(txn, event)
        elif event.type == EventTypes.Topic:
            self._store_room_topic_txn(txn, event)
        elif event.type == EventTypes.Redaction:
            self._store_redaction(txn, event)

        event_dict = {
            k: v
            for k, v in event.get_dict().items()
            if k not in [
                "redacted",
                "redacted_because",
            ]
        }

        self._simple_insert_txn(
            txn,
            table="event_json",
            values={
                "event_id": event.event_id,
                "room_id": event.room_id,
                "internal_metadata": metadata_json,
                "json": encode_canonical_json(event_dict).decode("UTF-8"),
            },
        )

        content = encode_canonical_json(
            event.content
        ).decode("UTF-8")

        vals = {
            "topological_ordering": event.depth,
            "event_id": event.event_id,
            "type": event.type,
            "room_id": event.room_id,
            "content": content,
            "processed": True,
            "outlier": outlier,
            "depth": event.depth,
        }

        unrec = {
            k: v
            for k, v in event.get_dict().items()
            if k not in vals.keys() and k not in [
                "redacted",
                "redacted_because",
                "signatures",
                "hashes",
                "prev_events",
            ]
        }

        vals["unrecognized_keys"] = encode_canonical_json(
            unrec
        ).decode("UTF-8")

        sql = (
            "INSERT INTO events"
            " (stream_ordering, topological_ordering, event_id, type,"
            " room_id, content, processed, outlier, depth)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
        )

        txn.execute(
            sql,
            (
                stream_ordering, event.depth, event.event_id, event.type,
                event.room_id, content, True, outlier, event.depth
            )
        )

        if context.rejected:
            self._store_rejections_txn(
                txn, event.event_id, context.rejected
            )

        for hash_alg, hash_base64 in event.hashes.items():
            hash_bytes = decode_base64(hash_base64)
            self._store_event_content_hash_txn(
                txn, event.event_id, hash_alg, hash_bytes,
            )

        for prev_event_id, prev_hashes in event.prev_events:
            for alg, hash_base64 in prev_hashes.items():
                hash_bytes = decode_base64(hash_base64)
                self._store_prev_event_hash_txn(
                    txn, event.event_id, prev_event_id, alg,
                    hash_bytes
                )

        self._simple_insert_many_txn(
            txn,
            table="event_auth",
            values=[
                {
                    "event_id": event.event_id,
                    "room_id": event.room_id,
                    "auth_id": auth_id,
                }
                for auth_id, _ in event.auth_events
            ],
        )

        (ref_alg, ref_hash_bytes) = compute_event_reference_hash(event)
        self._store_event_reference_hash_txn(
            txn, event.event_id, ref_alg, ref_hash_bytes
        )

        if event.is_state():
            vals = {
                "event_id": event.event_id,
                "room_id": event.room_id,
                "type": event.type,
                "state_key": event.state_key,
            }

            # TODO: How does this work with backfilling?
            if hasattr(event, "replaces_state"):
                vals["prev_state"] = event.replaces_state

            self._simple_insert_txn(
                txn,
                "state_events",
                vals,
            )

            self._simple_insert_many_txn(
                txn,
                table="event_edges",
                values=[
                    {
                        "event_id": event.event_id,
                        "prev_event_id": e_id,
                        "room_id": event.room_id,
                        "is_state": True,
                    }
                    for e_id, h in event.prev_state
                ],
            )

            if is_new_state and not context.rejected:
                self._simple_upsert_txn(
                    txn,
                    "current_state_events",
                    keyvalues={
                        "room_id": event.room_id,
                        "type": event.type,
                        "state_key": event.state_key,
                    },
                    values={
                        "event_id": event.event_id,
                    }
                )

        return

    def _store_redaction(self, txn, event):
        # invalidate the cache for the redacted event
        txn.call_after(self._invalidate_get_event_cache, event.redacts)
        txn.execute(
            "INSERT INTO redactions (event_id, redacts) VALUES (?,?)",
            (event.event_id, event.redacts)
        )

    def have_events(self, event_ids):
        """Given a list of event ids, check if we have already processed them.

        Returns:
            dict: Has an entry for each event id we already have seen. Maps to
            the rejected reason string if we rejected the event, else maps to
            None.
        """
        if not event_ids:
            return defer.succeed({})

        def f(txn):
            sql = (
                "SELECT e.event_id, reason FROM events as e "
                "LEFT JOIN rejections as r ON e.event_id = r.event_id "
                "WHERE e.event_id = ?"
            )

            res = {}
            for event_id in event_ids:
                txn.execute(sql, (event_id,))
                row = txn.fetchone()
                if row:
                    _, rejected = row
                    res[event_id] = rejected

            return res

        return self.runInteraction(
            "have_events", f,
        )
