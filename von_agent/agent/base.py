"""
Copyright 2017-2018 Government of Canada - Public Services and Procurement Canada - buyandsell.gc.ca

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""


import asyncio
import json
import logging

from typing import Union

from indy import ledger
from indy.error import IndyError, ErrorCode
from von_agent.cache import RevoCacheEntry, CRED_DEF_CACHE, REVO_CACHE, SCHEMA_CACHE
from von_agent.error import (
    AbsentCredDef,
    AbsentRevReg,
    AbsentSchema,
    BadLedgerTxn,
    ClosedPool,
    CorruptWallet)
from von_agent.nodepool import NodePool
from von_agent.util import schema_id, SchemaKey, schema_key
from von_agent.wallet import Wallet


LOGGER = logging.getLogger(__name__)


class _BaseAgent:
    """
    Base class for common agent functionality. A VON agent has a wallet and a
    node pool.  It has a role and a cryptonym, and can interact via indy-sdk
    with the distributed ledger that its node pool operates.
    """

    def __init__(self, wallet: Wallet) -> None:
        """
        Initializer for agent. Retain wallet.

        Raise AbsentWallet if wallet is not yet created.

        :param wallet: wallet for agent use
        """

        LOGGER.debug('_BaseAgent.__init__ >>> wallet: %s', wallet)

        self._wallet = wallet

        LOGGER.debug('_BaseAgent.__init__ <<<')

    @property
    def pool(self) -> NodePool:
        """
        Accessor for node pool.

        :return: node pool
        """

        return self.wallet.pool

    @property
    def wallet(self) -> Wallet:
        """
        Accessor for wallet.

        :return: wallet
        """

        return self._wallet

    @wallet.setter
    def wallet(self, value: Wallet) -> None:
        """
        Set wallet.

        :param value: wallet
        """

        self._wallet = value

    @property
    def did(self) -> str:
        """
        Accessor for agent DID.

        :return: agent DID
        """

        return self.wallet.did

    @property
    def verkey(self) -> str:
        """
        Accessor for agent verification key.

        :return: agent verification key
        """

        return self.wallet.verkey

    async def __aenter__(self) -> '_BaseAgent':
        """
        Context manager entry; open wallet.
        For use in monolithic call opening, using, and closing the agent.

        :return: current object
        """

        LOGGER.debug('_BaseAgent.__aenter__ >>>')

        rv = await self.open()

        LOGGER.debug('_BaseAgent.__aenter__ <<<')
        return rv

    async def open(self) -> '_BaseAgent':
        """
        Context manager entry; open wallet.
        For use when keeping agent open across multiple calls.

        :return: current object
        """

        LOGGER.debug('_BaseAgent.open >>>')

        await self.wallet.open()

        LOGGER.debug('_BaseAgent.open <<<')
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        """
        Context manager exit. Close wallet.
        For use in monolithic call opening, using, and closing the agent.

        :param exc_type:
        :param exc:
        :param traceback:
        """

        LOGGER.debug('_BaseAgent.__aexit__ >>> exc_type: %s, exc: %s, traceback: %s', exc_type, exc, traceback)

        await self.close()
        LOGGER.debug('_BaseAgent.__exit__ <<<')

    async def close(self) -> None:
        """
        Explicit exit. Close wallet.
        For use when keeping agent open across multiple calls.
        """

        LOGGER.debug('_BaseAgent.close >>>')

        await self.wallet.close()

        LOGGER.debug('_BaseAgent.close <<<')

    async def get_nym(self, did: str) -> str:
        """
        Get json cryptonym (including current verification key) for input (agent) DID from ledger.

        Raise BadLedgerTxn on failure.

        :param did: DID of cryptonym to fetch
        :return: cryptonym json
        """

        LOGGER.debug('_BaseAgent.get_nym >>> did: %s', did)

        rv = json.dumps({})
        get_nym_req = await ledger.build_get_nym_request(self.did, did)
        resp_json = await self._submit(get_nym_req)

        data_json = (json.loads(resp_json))['result']['data']  # it's double-encoded on the ledger
        if data_json:
            rv = data_json

        LOGGER.debug('_BaseAgent.get_nym <<< %s', rv)
        return rv

    @staticmethod
    def role() -> str:
        """
        Return the indy-sdk role for an agent in building its nym for the trust anchor to send to the ledger.

        :param agent: agent instance
        :return: role string
        """

        LOGGER.debug('_BaseAgent.role >>>')

        rv = 'TRUST_ANCHOR'
        LOGGER.debug('_BaseAgent.role <<< %s', rv)
        return rv

    async def _submit(self, req_json: str) -> str:
        """
        Submit (json) request to ledger; return (json) result.

        Raise ClosedPool if pool is not yet open, or BadLedgerTxn on failure.

        :param req_json: json of request to sign and submit
        :return: json response
        """

        LOGGER.debug('_BaseAgent._submit >>> json: %s', req_json)

        if not self.pool.handle:
            LOGGER.debug('_BaseAgent._submit <!< closed pool %s', self.pool.name)
            raise ClosedPool('Cannot submit request to closed pool {}'.format(self.pool.name))

        rv_json = await ledger.submit_request(self.pool.handle, req_json)
        await asyncio.sleep(0)

        resp = json.loads(rv_json)
        if ('op' in resp) and (resp['op'] in ('REQNACK', 'REJECT')):
            LOGGER.debug('_BaseAgent._submit: <!< ledger rejected request: %s', resp['reason'])
            raise BadLedgerTxn('Ledger rejected transaction request: {}'.format(resp['reason']))

        if 'reason' in resp and 'result' in resp and resp['result'].get('seqNo', None) is None:
            LOGGER.debug('_BaseAgent._submit: <!< response indicates no transaction: %s', resp['reason'])
            raise BadLedgerTxn('Response indicates no transaction: {}'.format(resp['reason']))

        LOGGER.debug('_BaseAgent._submit <<< %s', rv_json)
        return rv_json

    async def _sign_submit(self, req_json: str) -> str:
        """
        Sign and submit (json) request to ledger; return (json) result.

        Raise ClosedPool if pool is not yet open, CorruptWallet if existing wallet's
        pool is no longer extant, or BadLedgerTxn on any other failure.

        :param req_json: json of request to sign and submit
        :return: json response
        """

        LOGGER.debug('_BaseAgent._sign_submit >>> json: %s', req_json)

        if not self.pool.handle:
            LOGGER.debug('_BaseAgent._submit <!< closed pool %s', self.pool.name)
            raise ClosedPool('Cannot submit request to closed pool {}'.format(self.pool.name))

        try:
            rv_json = await ledger.sign_and_submit_request(self.pool.handle, self.wallet.handle, self.did, req_json)
            await asyncio.sleep(0)
        except IndyError as x_indy:
            if x_indy.error_code == ErrorCode.WalletIncompatiblePoolError:
                LOGGER.debug(
                    '_BaseAgent._sign_submit: <!< Corrupt wallet %s is not compatible with pool %s',
                    self.wallet.name,
                    self.pool.name)
                raise CorruptWallet(
                    'Corrupt wallet {} is not compatible with pool {}'.format(self.wallet.name, self.pool.name))
            else:
                LOGGER.debug(
                    '_BaseAgent._sign_submit: <!<  cannot sign/submit request for ledger: indy error code %s',
                    self.wallet.name)
                raise BadLedgerTxn('Cannot sign/submit request for ledger: indy error code {}'.format(
                    x_indy.error_code))

        resp = json.loads(rv_json)
        if ('op' in resp) and (resp['op'] in ('REQNACK', 'REJECT')):
            LOGGER.debug('_BaseAgent._sign_submit: ledger rejected request: %s', resp['reason'])
            raise BadLedgerTxn('Ledger rejected transaction request: {}'.format(resp['reason']))

        if 'reason' in resp and 'result' in resp and resp['result'].get('seqNo', None) is None:
            LOGGER.debug('_BaseAgent._sign_submit: <!< response indicates no transaction: %s', resp['reason'])
            raise BadLedgerTxn('Response indicates no transaction: {}'.format(resp['reason']))

        LOGGER.debug('_BaseAgent._sign_submit <<< %s', rv_json)
        return rv_json

    async def _get_rev_reg_def(self, rr_id: str) -> str:
        """
        Get revocation registry definition from ledger by its identifier. Raise AbsentRevReg
        for no such revocation registry, logging any error condition and raising BadLedgerTxn
        on bad request.

        Retrieve the revocation registry definition from the agent's revocation cache if it has it;
        cache it en passant if it does not (and such a revocation registry definition exists on the ledger).

        :param rr_id: (revocation registry) identifier string, of the format
            '<issuer-did>:4:<issuer-did>:3:CL:<schema-seq-no>:<tag>:CL_ACCUM:<tag>'
        :return: revocation registry definition json as retrieved from ledger
        """

        LOGGER.debug('_BaseAgent._get_rev_reg_def >>> rr_id: %s', rr_id)

        rv_json = json.dumps({})

        with REVO_CACHE.lock:
            revo_cache_entry = REVO_CACHE.get(rr_id, None)
            rr_def = revo_cache_entry.rev_reg_def if revo_cache_entry else None
            if rr_def:
                LOGGER.info('_BaseAgent._get_rev_reg_def: rev reg def for %s from cache', rr_id)
                rv_json = json.dumps(rr_def)
            else:
                get_rrd_req_json = await ledger.build_get_revoc_reg_def_request(self.did, rr_id)
                resp_json = await self._submit(get_rrd_req_json)
                try:
                    (_, rv_json) = await ledger.parse_get_revoc_reg_def_response(resp_json)
                    rr_def = json.loads(rv_json)
                except IndyError:  # ledger replied, but there is no such rev reg
                    LOGGER.debug('_BaseAgent._get_rev_reg_def: <!< no rev reg exists on %s', rr_id)
                    raise AbsentRevReg('No rev reg exists on {}'.format(rr_id))

                if revo_cache_entry is None:
                    REVO_CACHE[rr_id] = RevoCacheEntry(rr_def, None)
                else:
                    REVO_CACHE[rr_id].rev_reg_def = rr_def

        LOGGER.debug('_BaseAgent._get_rev_reg_def <<< %s', rv_json)
        return rv_json

    async def get_cred_def(self, cd_id: str) -> str:
        """
        Get credential definition from ledger by its identifier.

        Raise AbsentCredDef for no such credential definition, logging any error condition and raising
        BadLedgerTxn on bad request. Raise ClosedPool if cred def not in cache and pool is closed.

        Retrieve the credential definition from the agent's credential definition cache if it has it; cache it
        en passant if it does not (and if there is a corresponding credential definition on the ledger).

        :param cd_id: (credential definition) identifier string ('<issuer-did>:3:CL:<schema-seq-no>:<tag>')
        :return: credential definition json as retrieved from ledger, empty production for no such cred def
        """

        LOGGER.debug('_BaseAgent.get_cred_def >>> cd_id: %s', cd_id)

        rv_json = json.dumps({})

        with CRED_DEF_CACHE.lock:
            if cd_id in CRED_DEF_CACHE:
                LOGGER.info('_BaseAgent.get_cred_def: got cred def for %s from cache', cd_id)
                rv_json = json.dumps(CRED_DEF_CACHE[cd_id])
                LOGGER.debug('_BaseAgent.get_cred_def <<< %s', rv_json)
                return rv_json

            req_json = await ledger.build_get_cred_def_request(self.did, cd_id)
            resp_json = await self._submit(req_json)
            resp = json.loads(resp_json)
            if not ('result' in resp and resp['result'].get('data', None)):
                LOGGER.debug('_BaseAgent.get_cred_def: <!< no cred def exists on %s', cd_id)
                raise AbsentCredDef('No cred def exists on {}'.format(cd_id))
            try:
                (_, rv_json) = await ledger.parse_get_cred_def_response(resp_json)
            except IndyError:  # ledger replied, but there is no such cred def
                LOGGER.debug('_BaseAgent.get_cred_def: <!< no cred def exists on %s', cd_id)
                raise AbsentCredDef('No cred def exists on {}'.format(cd_id))
            CRED_DEF_CACHE[cd_id] = json.loads(rv_json)
            LOGGER.info('_BaseAgent.get_cred_def: got cred def %s from ledger', cd_id)

        LOGGER.debug('_BaseAgent.get_cred_def <<< %s', rv_json)
        return rv_json

    async def get_schema(self, index: Union[SchemaKey, int, str]) -> str:
        """
        Get schema from ledger by SchemaKey namedtuple (origin DID, name, version),
        sequence number, or schema identifier.

        Raise AbsentSchema for no such schema, logging any error condition and raising
        BadLedgerTxn on bad request.

        Retrieve the schema from the agent's schema cache if it has it; cache it
        en passant if it does not (and there is a corresponding schema on the ledger).

        :param index: schema key (origin DID, name, version), sequence number, or schema identifier
        :return: schema json, parsed from ledger
        """

        LOGGER.debug('_BaseAgent.get_schema >>> index: %s', index)

        rv_json = json.dumps({})
        with SCHEMA_CACHE.lock:
            if SCHEMA_CACHE.contains(index):
                LOGGER.info('_BaseAgent.get_schema: got schema %s from schema cache', index)
                rv_json = SCHEMA_CACHE[index]
                LOGGER.debug('_BaseAgent.get_schema <<< %s', rv_json)
                return json.dumps(rv_json)

            if isinstance(index, SchemaKey) or (isinstance(index, str) and ':2:' in index):
                s_id = schema_id(*index) if isinstance(index, SchemaKey) else index
                s_key = schema_key(s_id)
                req_json = await ledger.build_get_schema_request(self.did, s_id)
                resp_json = await self._submit(req_json)
                resp = json.loads(resp_json)

                if not ('result' in resp and resp['result'].get('data', {}).get('attr_names', None)):
                    LOGGER.debug('_BaseAgent.get_schema: <!< no schema exists on %s', index)
                    raise AbsentSchema('No schema exists on {}'.format(index))
                try:
                    (_, rv_json) = await ledger.parse_get_schema_response(resp_json)
                except IndyError:  # ledger replied, but there is no such schema
                    LOGGER.debug('_BaseAgent.get_schema: <!< no schema exists on %s', index)
                    raise AbsentSchema('No schema exists on {}'.format(index))
                SCHEMA_CACHE[s_key] = json.loads(rv_json)  # cache indexes by both txn# and schema key en passant
                LOGGER.info('_BaseAgent.get_schema: got schema %s from ledger', index)

            elif isinstance(index, (int, str)):  # :2: not in index - it's a stringified int txn no
                txn_json = await self.get_txn(int(index))
                txn = json.loads(txn_json)
                if txn.get('type', None) == '101':  # {} for no such txn; 101 marks indy-sdk schema txn type
                    rv_json = await self.get_schema(SchemaKey(
                        txn['metadata']['from'],
                        txn['data']['data']['name'],
                        txn['data']['data']['version']))
                else:
                    LOGGER.info('_BaseAgent.get_schema: no schema at seq #%s on ledger', index)

            else:
                LOGGER.debug('_BaseAgent.get_schema: <!< bad schema index type')
                raise AbsentSchema('Attempt to get schema on ({}) {} , must use schema key or an int'.format(
                    type(index),
                    index))

        LOGGER.debug('_BaseAgent.get_schema <<< %s', rv_json)
        return rv_json

    async def get_txn(self, txn: int) -> str:
        """
        Find a transaction on the distributed ledger by its sequence number.

        :param txn: transaction number
        :return: json sequence number of transaction, null for no match
        """

        LOGGER.debug('_BaseAgent.get_txn >>> txn: %s', txn)

        rv_json = json.dumps({})
        req_json = await ledger.build_get_txn_request(self.did, None, txn)
        resp = json.loads(await self._submit(req_json))

        rv_json = json.dumps(resp['result'].get('data', {}).get('txn', {}))
        LOGGER.debug('_BaseAgent.get_txn <<< %s', rv_json)
        return rv_json

    def __repr__(self) -> str:
        """
        Return representation for current object.

        :return: representation for current object
        """

        return '{}({})'.format(self.__class__.__name__, self.wallet)
