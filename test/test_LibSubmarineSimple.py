import logging
import os
import rlp
import sys
import unittest
from ethereum import config, transactions
from ethereum.tools import tester as t
from ethereum.utils import checksum_encode, normalize_address, sha3
from test_utils import rec_hex, rec_bin, deploy_solidity_contract_with_args

sys.path.append(
    os.path.join(os.path.dirname(__file__), '..', 'generate_commitment'))
import generate_submarine_commit

sys.path.append(
    os.path.join(os.path.dirname(__file__), '..', 'proveth', 'offchain'))
import proveth

root_repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../'))

COMMIT_PERIOD_LENGTH = 3
UNLOCK_AMOUNT = 1337000000000000000
OURGASLIMIT = 3712394
OURGASPRICE = 10**6
BASIC_SEND_GAS_LIMIT = 21000
extraTransactionFees = 100000000000000000
ACCOUNT_STARTING_BALANCE = 1000000000000000000000000

log = logging.getLogger('TestLibSubmarineSimple')
LOGFORMAT = "%(levelname)s:%(filename)s:%(lineno)s:%(funcName)s(): %(message)s"
log.setLevel(logging.getLevelName('INFO'))
logHandler = logging.StreamHandler(stream=sys.stdout)
logHandler.setFormatter(logging.Formatter(LOGFORMAT))
log.addHandler(logHandler)


class TestLibSubmarineSimple(unittest.TestCase):
    def setUp(self):
        config.config_metropolis['BLOCK_GAS_LIMIT'] = 2**60
        self.chain = t.Chain(env=config.Env(config=config.config_metropolis))
        self.chain.mine()
        contract_dir = os.path.abspath(
            os.path.join(root_repo_dir, 'contracts/'))
        os.chdir(root_repo_dir)

        self.verifier_contract = deploy_solidity_contract_with_args(
            chain=self.chain,
            solc_config_sources={
                'LibSubmarineSimple.sol': {
                    'urls':
                    [os.path.join(contract_dir, 'LibSubmarineSimple.sol')]
                },
                'SafeMath.sol': {
                    'urls': [os.path.join(contract_dir, 'SafeMath.sol')]
                },
                'SafeMath32.sol': {
                    'urls': [os.path.join(contract_dir, 'SafeMath32.sol')]
                },
                'proveth/ProvethVerifier.sol': {
                    'urls': [
                        os.path.join(contract_dir,
                                     'proveth/ProvethVerifier.sol')
                    ]
                },
                'proveth/RLP.sol': {
                    'urls': [os.path.join(contract_dir, 'proveth/RLP.sol')]
                }
            },
            allow_paths=root_repo_dir,
            contract_file='LibSubmarineSimple.sol',
            contract_name='LibSubmarineSimple',
            startgas=10**7,
            args=[COMMIT_PERIOD_LENGTH])

    def test_workflow(self):
        ##
        ## STARTING STATE
        ##
        ALICE_ADDRESS = t.a1
        ALICE_PRIVATE_KEY = t.k1

        log.info("Contract Address: {}".format(
            rec_hex(self.verifier_contract.address)))
        log.info("State: Starting A1 has {} and has address {}".format(
            self.chain.head_state.get_balance(rec_hex(ALICE_ADDRESS)),
            rec_hex(ALICE_ADDRESS)))

        self.chain.mine(1)

        ##
        ## GENERATE UNLOCK AND BROADCAST TX, THEN BROADCAST JUST COMMIT TX
        ##
        addressB, commit, witness, unlock_tx_hex = generate_submarine_commit.generateCommitAddress(
            normalize_address(rec_hex(ALICE_ADDRESS)),
            normalize_address(rec_hex(self.verifier_contract.address)),
            UNLOCK_AMOUNT, b'', OURGASPRICE, OURGASLIMIT)
        log.info("Precomputed address of commit target: {}".format(addressB))

        unlock_tx_info = rlp.decode(rec_bin(unlock_tx_hex))
        log.info("Unlock tx hex object: {}".format(rec_hex(unlock_tx_info)))

        unlock_tx_object = transactions.Transaction(
            int.from_bytes(unlock_tx_info[0], byteorder="big"),  # nonce;
            int.from_bytes(unlock_tx_info[1], byteorder="big"),  # gasprice
            int.from_bytes(unlock_tx_info[2], byteorder="big"),  # startgas
            unlock_tx_info[3],  # to addr
            int.from_bytes(unlock_tx_info[4], byteorder="big"),  # value
            unlock_tx_info[5],  # data
            int.from_bytes(unlock_tx_info[6], byteorder="big"),  # v
            int.from_bytes(unlock_tx_info[7], byteorder="big"),  # r
            int.from_bytes(unlock_tx_info[8], byteorder="big")  # s
        )
        log.info("Unlock tx hash: {}".format(rec_hex(unlock_tx_object.hash)))

        commit_tx_object = transactions.Transaction(
            0, OURGASPRICE, BASIC_SEND_GAS_LIMIT, rec_bin(addressB),
            (UNLOCK_AMOUNT + extraTransactionFees),
            b'').sign(ALICE_PRIVATE_KEY)
        log.info("Commit TX Object: {}".format(
            str(commit_tx_object.to_dict())))
        log.info("Commit TX gas used Intrinsic: {}".format(
            str(commit_tx_object.intrinsic_gas_used)))
        commit_gas = int(self.chain.head_state.gas_used)

        self.chain.direct_tx(commit_tx_object)
        log.info("Commit TX Gas Used HeadState {}".format(
            self.chain.head_state.gas_used))

        self.chain.mine(4)

        ##
        ## CHECK STATE AFTER COMMIT TX
        ##
        commit_block_number, commit_block_index = self.chain.chain.get_tx_position(
            commit_tx_object)
        log.info("Commit Tx block number {} and tx block index {}".format(
            commit_block_number, commit_block_index))
        log.info("State: After commit A1 has {} and has address {}".format(
            self.chain.head_state.get_balance(rec_hex(ALICE_ADDRESS)),
            rec_hex(ALICE_ADDRESS)))
        log.info("State: After commit B has {} and has address {}".format(
            self.chain.head_state.get_balance(addressB), addressB))
        self.assertEqual(UNLOCK_AMOUNT + extraTransactionFees,
                         self.chain.head_state.get_balance(addressB))
        self.assertEqual(
            ACCOUNT_STARTING_BALANCE - (UNLOCK_AMOUNT + extraTransactionFees +
                                        BASIC_SEND_GAS_LIMIT * OURGASPRICE),
            self.chain.head_state.get_balance(rec_hex(ALICE_ADDRESS)))

        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(
            session_data, [False, False, 0, 0],
            "The contract should not know anything about the commit until after it's been revealed... "
        )

        finished_bool = self.verifier_contract.finished(rec_bin(commit))
        self.assertFalse(
            finished_bool,
            "The contract should not be finished before it's even begun.")

        ##
        ## GENERATE AND BROADCAST REVEAL TX
        ##
        assert (isinstance(witness, str))
        commit_block_object = self.chain.chain.get_block_by_number(
            commit_block_number)
        log.info("Block information: {}".format(
            str(commit_block_object.as_dict())))
        log.info("Block header: {}".format(
            str(commit_block_object.as_dict()['header'].as_dict())))
        log.info("Block transactions: {}".format(
            str(commit_block_object.as_dict()['transactions'][0].as_dict())))

        proveth_expected_block_format_dict = dict()
        proveth_expected_block_format_dict['parentHash'] = commit_block_object['prevhash']
        proveth_expected_block_format_dict['sha3Uncles'] = commit_block_object['uncles_hash']
        proveth_expected_block_format_dict['miner'] = commit_block_object['coinbase']
        proveth_expected_block_format_dict['stateRoot'] = commit_block_object['state_root']
        proveth_expected_block_format_dict['transactionsRoot'] = commit_block_object['tx_list_root']
        proveth_expected_block_format_dict['receiptsRoot'] = commit_block_object['receipts_root']
        proveth_expected_block_format_dict['logsBloom'] = commit_block_object['bloom']
        proveth_expected_block_format_dict['difficulty'] = commit_block_object['difficulty']
        proveth_expected_block_format_dict['number'] = commit_block_object['number']
        proveth_expected_block_format_dict['gasLimit'] = commit_block_object['gas_limit']
        proveth_expected_block_format_dict['gasUsed'] = commit_block_object['gas_used']
        proveth_expected_block_format_dict['timestamp'] = commit_block_object['timestamp']
        proveth_expected_block_format_dict['extraData'] = commit_block_object['extra_data']
        proveth_expected_block_format_dict['mixHash'] = commit_block_object['mixhash']
        proveth_expected_block_format_dict['nonce'] = commit_block_object['nonce']
        proveth_expected_block_format_dict['hash'] = commit_block_object.hash
        proveth_expected_block_format_dict['uncles'] = []

        # remember kids, when in doubt, rec_hex EVERYTHING
        proveth_expected_block_format_dict['transactions'] = ({
            "blockHash":          commit_block_object.hash,
            "blockNumber":        str(hex((commit_block_object['number']))),
            "from":               checksum_encode(ALICE_ADDRESS),
            "gas":                str(hex(commit_tx_object['startgas'])),
            "gasPrice":           str(hex(commit_tx_object['gasprice'])),
            "hash":               rec_hex(commit_tx_object['hash']),
            "input":              rec_hex(commit_tx_object['data']),
            "nonce":              str(hex(commit_tx_object['nonce'])),
            "to":                 checksum_encode(commit_tx_object['to']),
            "transactionIndex":   str(hex(0)),
            "value":              str(hex(commit_tx_object['value'])),
            "v":                  str(hex(commit_tx_object['v'])),
            "r":                  str(hex(commit_tx_object['r'])),
            "s":                  str(hex(commit_tx_object['s']))
        }, )

        #log.info(proveth_expected_block_format_dict['transactions'])
        commit_proof_blob = proveth.generate_proof_blob(
            proveth_expected_block_format_dict, commit_block_index)
        log.info("Proof Blob generate by proveth.py: {}".format(
            rec_hex(commit_proof_blob)))

        # Solidity Event log listener
        def _event_listener(llog):
            log.info('Solidity Event listener log fire: {}'.format(str(llog)))
            log.info('Solidity Event listener log fire hex: {}'.format(
                str(rec_hex(llog['data']))))

        self.chain.head_state.log_listeners.append(_event_listener)
        _unlockExtraData = b''  # In this example we dont have any extra embedded data as part of the unlock TX

        # need the unsigned TX hash for ECRecover in the reveal function
        unlock_tx_unsigned_object = transactions.UnsignedTransaction(
            nonce=unlock_tx_object['nonce'],
            gasprice=unlock_tx_object['gasprice'],
            startgas=unlock_tx_object['startgas'],
            to=unlock_tx_object['to'],
            value=unlock_tx_object['value'],
            data=unlock_tx_object['data'])
        unlock_tx_unsigned_hash = sha3(
            rlp.encode(unlock_tx_unsigned_object,
                       transactions.UnsignedTransaction))

        self.verifier_contract.reveal(
            #print(
            commit_block_number,  # uint32 _commitBlockNumber,
            _unlockExtraData,  # bytes _commitData,
            UNLOCK_AMOUNT,  # uint256 _unlockAmount,
            rec_bin(witness),  # bytes32 _witness,
            OURGASPRICE,  # uint256 _unlockGasPrice,
            OURGASLIMIT,  # uint256 _unlockGasLimit,
            unlock_tx_unsigned_hash,  # bytes32 _unlockTXHash,
            commit_proof_blob,  # bytes _proofBlob
            sender=ALICE_PRIVATE_KEY)
        log.info("Reveal TX Gas Used HeadState {}".format(
            self.chain.head_state.gas_used))
        reveal_gas = int(self.chain.head_state.gas_used)

        self.chain.mine(1)

        ##
        ## CHECK STATE AFTER REVEAL TX
        ##
        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(
            session_data, [True, False, UNLOCK_AMOUNT, 0],
            "After the Reveal, the state should report revealed but not unlocked."
        )
        finished_bool = self.verifier_contract.finished(rec_bin(commit))
        self.assertFalse(
            finished_bool,
            "The contract is only revealed, not unlocked and therefore finished."
        )

        ##
        ## BROADCAST UNLOCK
        ##
        self.chain.direct_tx(unlock_tx_object)
        log.info("Unlock TX Gas Used HeadState {}".format(
            self.chain.head_state.gas_used))
        unlock_gas = int(self.chain.head_state.gas_used)

        ##
        ## CHECK STATE AFTER UNLOCK
        ##
        log.info("State: After unlock B has {} and has address {}".format(
            self.chain.head_state.get_balance(addressB), addressB))

        self.assertLess(
            self.chain.head_state.get_balance(addressB),
            UNLOCK_AMOUNT + extraTransactionFees,
            "Address B should send along the money and have almost 0 money left."
        )
        self.assertEqual(
            999998562999979000000000,
            self.chain.head_state.get_balance(rec_hex(ALICE_ADDRESS)))

        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(
            session_data, [True, True, UNLOCK_AMOUNT, UNLOCK_AMOUNT],
            "State does not match expected value after unlock.")

        finished_bool = self.verifier_contract.finished(rec_bin(commit))
        self.assertTrue(finished_bool,
                        "After unlock, contract should be finished.")

        sumGas = commit_gas + reveal_gas + unlock_gas
        log.info("Final Gas Estimation {}".format(str(sumGas)))


    # Unlocking before revealing should still yield a usable result
    def test_unlock_before_reveal(self):
##
        ## STARTING STATE
        ##
        ALICE_ADDRESS = t.a1
        ALICE_PRIVATE_KEY = t.k1

        self.chain.mine(1)

        ##
        ## GENERATE UNLOCK TX
        ##
        addressB, commit, witness, unlock_tx_hex = generate_submarine_commit.generateCommitAddress(
            normalize_address(rec_hex(ALICE_ADDRESS)),
            normalize_address(rec_hex(self.verifier_contract.address)),
            UNLOCK_AMOUNT, b'', OURGASPRICE, OURGASLIMIT)

        unlock_tx_info = rlp.decode(rec_bin(unlock_tx_hex))

        unlock_tx_object = transactions.Transaction(
            int.from_bytes(unlock_tx_info[0], byteorder="big"),  # nonce;
            int.from_bytes(unlock_tx_info[1], byteorder="big"),  # gasprice
            int.from_bytes(unlock_tx_info[2], byteorder="big"),  # startgas
            unlock_tx_info[3],  # to addr
            int.from_bytes(unlock_tx_info[4], byteorder="big"),  # value
            unlock_tx_info[5],  # data
            int.from_bytes(unlock_tx_info[6], byteorder="big"),  # v
            int.from_bytes(unlock_tx_info[7], byteorder="big"),  # r
            int.from_bytes(unlock_tx_info[8], byteorder="big")  # s
        )


        ##
        ## GENERATE COMMIT
        ##
        commit_tx_object = transactions.Transaction(
            0, OURGASPRICE, BASIC_SEND_GAS_LIMIT, rec_bin(addressB),
            (UNLOCK_AMOUNT + extraTransactionFees),
            b'').sign(ALICE_PRIVATE_KEY)

        self.chain.direct_tx(commit_tx_object)

        self.chain.mine(4)

        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(session_data, [False, False, 0, 0])

        finished_bool = self.verifier_contract.finished(rec_bin(commit))
        self.assertFalse(
            finished_bool,
            "The contract should not be finished until after the reveal.")

        commit_block_number, commit_block_index = self.chain.chain.get_tx_position(commit_tx_object)

        ##
        ## BROADCAST UNLOCK BEFORE REVEAL
        ##
        self.chain.direct_tx(unlock_tx_object)

        ##
        ## CHECK STATE AFTER UNLOCK
        ##
        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(
            session_data, [False, True, 0, UNLOCK_AMOUNT],
            "State does not match expected value after unlock.")

        finished_bool = self.verifier_contract.finished(rec_bin(commit))
        self.assertFalse(finished_bool)

        ##
        ## GENERATE AND BROADCAST REVEAL TX
        ##
        assert (isinstance(witness, str))
        commit_block_object = self.chain.chain.get_block_by_number(
            commit_block_number)
        log.info("Block information: {}".format(
            str(commit_block_object.as_dict())))
        log.info("Block header: {}".format(
            str(commit_block_object.as_dict()['header'].as_dict())))
        log.info("Block transactions: {}".format(
            str(commit_block_object.as_dict()['transactions'][0].as_dict())))

        proveth_expected_block_format_dict = dict()
        proveth_expected_block_format_dict['parentHash'] = commit_block_object['prevhash']
        proveth_expected_block_format_dict['sha3Uncles'] = commit_block_object['uncles_hash']
        proveth_expected_block_format_dict['miner'] = commit_block_object['coinbase']
        proveth_expected_block_format_dict['stateRoot'] = commit_block_object['state_root']
        proveth_expected_block_format_dict['transactionsRoot'] = commit_block_object['tx_list_root']
        proveth_expected_block_format_dict['receiptsRoot'] = commit_block_object['receipts_root']
        proveth_expected_block_format_dict['logsBloom'] = commit_block_object['bloom']
        proveth_expected_block_format_dict['difficulty'] = commit_block_object['difficulty']
        proveth_expected_block_format_dict['number'] = commit_block_object['number']
        proveth_expected_block_format_dict['gasLimit'] = commit_block_object['gas_limit']
        proveth_expected_block_format_dict['gasUsed'] = commit_block_object['gas_used']
        proveth_expected_block_format_dict['timestamp'] = commit_block_object['timestamp']
        proveth_expected_block_format_dict['extraData'] = commit_block_object['extra_data']
        proveth_expected_block_format_dict['mixHash'] = commit_block_object['mixhash']
        proveth_expected_block_format_dict['nonce'] = commit_block_object['nonce']
        proveth_expected_block_format_dict['hash'] = commit_block_object.hash
        proveth_expected_block_format_dict['uncles'] = []

        # remember kids, when in doubt, rec_hex EVERYTHING
        proveth_expected_block_format_dict['transactions'] = ({
            "blockHash":          commit_block_object.hash,
            "blockNumber":        str(hex((commit_block_object['number']))),
            "from":               checksum_encode(ALICE_ADDRESS),
            "gas":                str(hex(commit_tx_object['startgas'])),
            "gasPrice":           str(hex(commit_tx_object['gasprice'])),
            "hash":               rec_hex(commit_tx_object['hash']),
            "input":              rec_hex(commit_tx_object['data']),
            "nonce":              str(hex(commit_tx_object['nonce'])),
            "to":                 checksum_encode(commit_tx_object['to']),
            "transactionIndex":   str(hex(0)),
            "value":              str(hex(commit_tx_object['value'])),
            "v":                  str(hex(commit_tx_object['v'])),
            "r":                  str(hex(commit_tx_object['r'])),
            "s":                  str(hex(commit_tx_object['s']))
        }, )

        #log.info(proveth_expected_block_format_dict['transactions'])
        commit_proof_blob = proveth.generate_proof_blob(
            proveth_expected_block_format_dict, commit_block_index)
        log.info("Proof Blob generate by proveth.py: {}".format(
            rec_hex(commit_proof_blob)))

        # Solidity Event log listener
        def _event_listener(llog):
            log.info('Solidity Event listener log fire: {}'.format(str(llog)))
            log.info('Solidity Event listener log fire hex: {}'.format(
                str(rec_hex(llog['data']))))

        self.chain.head_state.log_listeners.append(_event_listener)
        _unlockExtraData = b''  # In this example we dont have any extra embedded data as part of the unlock TX

        # need the unsigned TX hash for ECRecover in the reveal function
        unlock_tx_unsigned_object = transactions.UnsignedTransaction(
            nonce=unlock_tx_object['nonce'],
            gasprice=unlock_tx_object['gasprice'],
            startgas=unlock_tx_object['startgas'],
            to=unlock_tx_object['to'],
            value=unlock_tx_object['value'],
            data=unlock_tx_object['data'])
        unlock_tx_unsigned_hash = sha3(
            rlp.encode(unlock_tx_unsigned_object,
                       transactions.UnsignedTransaction))

        self.verifier_contract.reveal(
            commit_block_number,  # uint32 _commitBlockNumber,
            _unlockExtraData,  # bytes _commitData,
            UNLOCK_AMOUNT,  # uint256 _unlockAmount,
            rec_bin(witness),  # bytes32 _witness,
            OURGASPRICE,  # uint256 _unlockGasPrice,
            OURGASLIMIT,  # uint256 _unlockGasLimit,
            unlock_tx_unsigned_hash,  # bytes32 _unlockTXHash,
            commit_proof_blob,  # bytes _proofBlob
            sender=ALICE_PRIVATE_KEY)
        log.info("Reveal TX Gas Used HeadState {}".format(
            self.chain.head_state.gas_used))
        reveal_gas = int(self.chain.head_state.gas_used)

        self.chain.mine(1)

        ##
        ## CHECK STATE AFTER REVEAL TX
        ##
        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(
            session_data, [True, True, UNLOCK_AMOUNT, UNLOCK_AMOUNT],
            "After the Reveal, the state should report both revealed and unlocked."
        )
        finished_bool = self.verifier_contract.finished(rec_bin(commit))
        self.assertTrue(
            finished_bool,
            "The contract was unlocked first and then revealed, it should be finished"
        )

    # Spam collisions should not happen with the unlock TX because of infinitesimally small probability of collision
    # But even if they do it should *still* not be an issue
    def test_spam_unlock_small_spam(self):
        ##
        ## STARTING STATE
        ##
        ALICE_ADDRESS = t.a1
        ALICE_PRIVATE_KEY = t.k1
        SPAM_PRIVATE_KEY_MALLORY = t.k7


        self.chain.mine(1)

        ##
        ## GENERATE UNLOCK TX
        ##
        addressB, commit, witness, unlock_tx_hex = generate_submarine_commit.generateCommitAddress(
            normalize_address(rec_hex(ALICE_ADDRESS)),
            normalize_address(rec_hex(self.verifier_contract.address)),
            UNLOCK_AMOUNT, b'', OURGASPRICE, OURGASLIMIT)

        unlock_tx_info = rlp.decode(rec_bin(unlock_tx_hex))

        unlock_tx_object = transactions.Transaction(
            int.from_bytes(unlock_tx_info[0], byteorder="big"),  # nonce;
            int.from_bytes(unlock_tx_info[1], byteorder="big"),  # gasprice
            int.from_bytes(unlock_tx_info[2], byteorder="big"),  # startgas
            unlock_tx_info[3],                                   # to addr
            int.from_bytes(unlock_tx_info[4], byteorder="big"),  # value
            unlock_tx_info[5],                                   # data
            int.from_bytes(unlock_tx_info[6], byteorder="big"),  # v
            int.from_bytes(unlock_tx_info[7], byteorder="big"),  # r
            int.from_bytes(unlock_tx_info[8], byteorder="big")   # s
        )

        ##
        ## SPAM THE UNLOCK FUNCTION
        ##
        SPAM_AMOUNT = 3
        spam_tx_object = transactions.Transaction(
            0,
            OURGASPRICE,
            OURGASLIMIT,
            normalize_address(rec_hex(self.verifier_contract.address)),
            SPAM_AMOUNT,
            unlock_tx_object[5]).sign(SPAM_PRIVATE_KEY_MALLORY)

        self.chain.direct_tx(spam_tx_object)
        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(session_data, [False, True, 0, SPAM_AMOUNT])
        self.chain.mine(1)

        ##
        ## GENERATE COMMIT
        ##
        commit_tx_object = transactions.Transaction(
            0, OURGASPRICE, BASIC_SEND_GAS_LIMIT, rec_bin(addressB),
            (UNLOCK_AMOUNT + extraTransactionFees),
            b'').sign(ALICE_PRIVATE_KEY)

        self.chain.direct_tx(commit_tx_object)

        self.chain.mine(4)

        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(session_data, [False, True, 0, SPAM_AMOUNT])

        finished_bool = self.verifier_contract.finished(rec_bin(commit))
        self.assertFalse(
            finished_bool,
            "The contract should not be finished until after the reveal.")

        commit_block_number, commit_block_index = self.chain.chain.get_tx_position(commit_tx_object)

        ##
        ## BROADCAST UNLOCK BEFORE REVEAL
        ##
        self.chain.direct_tx(unlock_tx_object)

        ##
        ## CHECK STATE AFTER UNLOCK
        ##
        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(
            session_data, [False, True, 0, UNLOCK_AMOUNT],
            "State does not match expected value after unlock.")

        finished_bool = self.verifier_contract.finished(rec_bin(commit))
        self.assertFalse(finished_bool)

        ##
        ## GENERATE AND BROADCAST REVEAL TX
        ##
        assert (isinstance(witness, str))
        commit_block_object = self.chain.chain.get_block_by_number(
            commit_block_number)
        log.info("Block information: {}".format(
            str(commit_block_object.as_dict())))
        log.info("Block header: {}".format(
            str(commit_block_object.as_dict()['header'].as_dict())))
        log.info("Block transactions: {}".format(
            str(commit_block_object.as_dict()['transactions'][0].as_dict())))

        proveth_expected_block_format_dict = dict()
        proveth_expected_block_format_dict['parentHash'] = commit_block_object['prevhash']
        proveth_expected_block_format_dict['sha3Uncles'] = commit_block_object['uncles_hash']
        proveth_expected_block_format_dict['miner'] = commit_block_object['coinbase']
        proveth_expected_block_format_dict['stateRoot'] = commit_block_object['state_root']
        proveth_expected_block_format_dict['transactionsRoot'] = commit_block_object['tx_list_root']
        proveth_expected_block_format_dict['receiptsRoot'] = commit_block_object['receipts_root']
        proveth_expected_block_format_dict['logsBloom'] = commit_block_object['bloom']
        proveth_expected_block_format_dict['difficulty'] = commit_block_object['difficulty']
        proveth_expected_block_format_dict['number'] = commit_block_object['number']
        proveth_expected_block_format_dict['gasLimit'] = commit_block_object['gas_limit']
        proveth_expected_block_format_dict['gasUsed'] = commit_block_object['gas_used']
        proveth_expected_block_format_dict['timestamp'] = commit_block_object['timestamp']
        proveth_expected_block_format_dict['extraData'] = commit_block_object['extra_data']
        proveth_expected_block_format_dict['mixHash'] = commit_block_object['mixhash']
        proveth_expected_block_format_dict['nonce'] = commit_block_object['nonce']
        proveth_expected_block_format_dict['hash'] = commit_block_object.hash
        proveth_expected_block_format_dict['uncles'] = []

        # remember kids, when in doubt, rec_hex EVERYTHING
        proveth_expected_block_format_dict['transactions'] = ({
            "blockHash":          commit_block_object.hash,
            "blockNumber":        str(hex((commit_block_object['number']))),
            "from":               checksum_encode(ALICE_ADDRESS),
            "gas":                str(hex(commit_tx_object['startgas'])),
            "gasPrice":           str(hex(commit_tx_object['gasprice'])),
            "hash":               rec_hex(commit_tx_object['hash']),
            "input":              rec_hex(commit_tx_object['data']),
            "nonce":              str(hex(commit_tx_object['nonce'])),
            "to":                 checksum_encode(commit_tx_object['to']),
            "transactionIndex":   str(hex(0)),
            "value":              str(hex(commit_tx_object['value'])),
            "v":                  str(hex(commit_tx_object['v'])),
            "r":                  str(hex(commit_tx_object['r'])),
            "s":                  str(hex(commit_tx_object['s']))
        }, )

        #log.info(proveth_expected_block_format_dict['transactions'])
        commit_proof_blob = proveth.generate_proof_blob(
            proveth_expected_block_format_dict, commit_block_index)
        log.info("Proof Blob generate by proveth.py: {}".format(
            rec_hex(commit_proof_blob)))

        # Solidity Event log listener
        def _event_listener(llog):
            log.info('Solidity Event listener log fire: {}'.format(str(llog)))
            log.info('Solidity Event listener log fire hex: {}'.format(
                str(rec_hex(llog['data']))))

        self.chain.head_state.log_listeners.append(_event_listener)
        _unlockExtraData = b''  # In this example we dont have any extra embedded data as part of the unlock TX

        # need the unsigned TX hash for ECRecover in the reveal function
        unlock_tx_unsigned_object = transactions.UnsignedTransaction(
            nonce=unlock_tx_object['nonce'],
            gasprice=unlock_tx_object['gasprice'],
            startgas=unlock_tx_object['startgas'],
            to=unlock_tx_object['to'],
            value=unlock_tx_object['value'],
            data=unlock_tx_object['data'])
        unlock_tx_unsigned_hash = sha3(
            rlp.encode(unlock_tx_unsigned_object,
                       transactions.UnsignedTransaction))

        self.verifier_contract.reveal(
            commit_block_number,  # uint32 _commitBlockNumber,
            _unlockExtraData,  # bytes _commitData,
            UNLOCK_AMOUNT,  # uint256 _unlockAmount,
            rec_bin(witness),  # bytes32 _witness,
            OURGASPRICE,  # uint256 _unlockGasPrice,
            OURGASLIMIT,  # uint256 _unlockGasLimit,
            unlock_tx_unsigned_hash,  # bytes32 _unlockTXHash,
            commit_proof_blob,  # bytes _proofBlob
            sender=ALICE_PRIVATE_KEY)
        log.info("Reveal TX Gas Used HeadState {}".format(
            self.chain.head_state.gas_used))
        reveal_gas = int(self.chain.head_state.gas_used)

        self.chain.mine(1)

        ##
        ## CHECK STATE AFTER REVEAL TX
        ##
        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(
            session_data, [True, True, UNLOCK_AMOUNT, UNLOCK_AMOUNT],
            "After the Reveal, the state should report both revealed and unlocked."
        )
        finished_bool = self.verifier_contract.finished(rec_bin(commit))
        self.assertTrue(
            finished_bool,
            "The contract was unlocked first and then revealed, it should be finished"
        )

    # Spam collisions should not happen with the unlock TX because of infinitesimally small probability of collision
    # But even if they do it should *still* not be an issue
    def test_spam_unlock_large_spam(self):
        ##
        ## STARTING STATE
        ##
        ALICE_ADDRESS = t.a1
        ALICE_PRIVATE_KEY = t.k1
        SPAM_PRIVATE_KEY_MALLORY = t.k7


        self.chain.mine(1)

        ##
        ## GENERATE UNLOCK TX
        ##
        addressB, commit, witness, unlock_tx_hex = generate_submarine_commit.generateCommitAddress(
            normalize_address(rec_hex(ALICE_ADDRESS)),
            normalize_address(rec_hex(self.verifier_contract.address)),
            UNLOCK_AMOUNT, b'', OURGASPRICE, OURGASLIMIT)

        unlock_tx_info = rlp.decode(rec_bin(unlock_tx_hex))

        unlock_tx_object = transactions.Transaction(
            int.from_bytes(unlock_tx_info[0], byteorder="big"),  # nonce;
            int.from_bytes(unlock_tx_info[1], byteorder="big"),  # gasprice
            int.from_bytes(unlock_tx_info[2], byteorder="big"),  # startgas
            unlock_tx_info[3],                                   # to addr
            int.from_bytes(unlock_tx_info[4], byteorder="big"),  # value
            unlock_tx_info[5],                                   # data
            int.from_bytes(unlock_tx_info[6], byteorder="big"),  # v
            int.from_bytes(unlock_tx_info[7], byteorder="big"),  # r
            int.from_bytes(unlock_tx_info[8], byteorder="big")   # s
        )

        ##
        ## SPAM THE UNLOCK FUNCTION
        ##
        SPAM_AMOUNT = UNLOCK_AMOUNT + 3235
        spam_tx_object = transactions.Transaction(
            0,
            OURGASPRICE,
            OURGASLIMIT,
            normalize_address(rec_hex(self.verifier_contract.address)),
            SPAM_AMOUNT,
            unlock_tx_object[5]).sign(SPAM_PRIVATE_KEY_MALLORY)

        self.chain.direct_tx(spam_tx_object)
        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(session_data, [False, True, 0, SPAM_AMOUNT])
        self.chain.mine(1)

        ##
        ## GENERATE COMMIT
        ##
        commit_tx_object = transactions.Transaction(
            0, OURGASPRICE, BASIC_SEND_GAS_LIMIT, rec_bin(addressB),
            (UNLOCK_AMOUNT + extraTransactionFees),
            b'').sign(ALICE_PRIVATE_KEY)

        self.chain.direct_tx(commit_tx_object)

        self.chain.mine(4)

        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(session_data, [False, True, 0, SPAM_AMOUNT])

        finished_bool = self.verifier_contract.finished(rec_bin(commit))
        self.assertFalse(
            finished_bool,
            "The contract should not be finished until after the reveal.")

        commit_block_number, commit_block_index = self.chain.chain.get_tx_position(commit_tx_object)

        ##
        ## BROADCAST UNLOCK (this should cause an exception since someone else donated money to your cause)
        ##
        self.assertRaises(t.TransactionFailed, self.chain.direct_tx, (unlock_tx_object))

        ##
        ## CHECK STATE AFTER UNLOCK
        ##
        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(
            session_data, [False, True, 0, SPAM_AMOUNT],
            "State does not match expected value after unlock.")

        finished_bool = self.verifier_contract.finished(rec_bin(commit))
        self.assertFalse(finished_bool)

        ##
        ## GENERATE AND BROADCAST REVEAL TX
        ##
        assert (isinstance(witness, str))
        commit_block_object = self.chain.chain.get_block_by_number(
            commit_block_number)
        log.info("Block information: {}".format(
            str(commit_block_object.as_dict())))
        log.info("Block header: {}".format(
            str(commit_block_object.as_dict()['header'].as_dict())))
        log.info("Block transactions: {}".format(
            str(commit_block_object.as_dict()['transactions'][0].as_dict())))

        proveth_expected_block_format_dict = dict()
        proveth_expected_block_format_dict['parentHash'] = commit_block_object['prevhash']
        proveth_expected_block_format_dict['sha3Uncles'] = commit_block_object['uncles_hash']
        proveth_expected_block_format_dict['miner'] = commit_block_object['coinbase']
        proveth_expected_block_format_dict['stateRoot'] = commit_block_object['state_root']
        proveth_expected_block_format_dict['transactionsRoot'] = commit_block_object['tx_list_root']
        proveth_expected_block_format_dict['receiptsRoot'] = commit_block_object['receipts_root']
        proveth_expected_block_format_dict['logsBloom'] = commit_block_object['bloom']
        proveth_expected_block_format_dict['difficulty'] = commit_block_object['difficulty']
        proveth_expected_block_format_dict['number'] = commit_block_object['number']
        proveth_expected_block_format_dict['gasLimit'] = commit_block_object['gas_limit']
        proveth_expected_block_format_dict['gasUsed'] = commit_block_object['gas_used']
        proveth_expected_block_format_dict['timestamp'] = commit_block_object['timestamp']
        proveth_expected_block_format_dict['extraData'] = commit_block_object['extra_data']
        proveth_expected_block_format_dict['mixHash'] = commit_block_object['mixhash']
        proveth_expected_block_format_dict['nonce'] = commit_block_object['nonce']
        proveth_expected_block_format_dict['hash'] = commit_block_object.hash
        proveth_expected_block_format_dict['uncles'] = []

        # remember kids, when in doubt, rec_hex EVERYTHING
        proveth_expected_block_format_dict['transactions'] = ({
            "blockHash":          commit_block_object.hash,
            "blockNumber":        str(hex((commit_block_object['number']))),
            "from":               checksum_encode(ALICE_ADDRESS),
            "gas":                str(hex(commit_tx_object['startgas'])),
            "gasPrice":           str(hex(commit_tx_object['gasprice'])),
            "hash":               rec_hex(commit_tx_object['hash']),
            "input":              rec_hex(commit_tx_object['data']),
            "nonce":              str(hex(commit_tx_object['nonce'])),
            "to":                 checksum_encode(commit_tx_object['to']),
            "transactionIndex":   str(hex(0)),
            "value":              str(hex(commit_tx_object['value'])),
            "v":                  str(hex(commit_tx_object['v'])),
            "r":                  str(hex(commit_tx_object['r'])),
            "s":                  str(hex(commit_tx_object['s']))
        }, )

        #log.info(proveth_expected_block_format_dict['transactions'])
        commit_proof_blob = proveth.generate_proof_blob(
            proveth_expected_block_format_dict, commit_block_index)
        log.info("Proof Blob generate by proveth.py: {}".format(
            rec_hex(commit_proof_blob)))

        # Solidity Event log listener
        def _event_listener(llog):
            log.info('Solidity Event listener log fire: {}'.format(str(llog)))
            log.info('Solidity Event listener log fire hex: {}'.format(
                str(rec_hex(llog['data']))))

        self.chain.head_state.log_listeners.append(_event_listener)
        _unlockExtraData = b''  # In this example we dont have any extra embedded data as part of the unlock TX

        # need the unsigned TX hash for ECRecover in the reveal function
        unlock_tx_unsigned_object = transactions.UnsignedTransaction(
            nonce=unlock_tx_object['nonce'],
            gasprice=unlock_tx_object['gasprice'],
            startgas=unlock_tx_object['startgas'],
            to=unlock_tx_object['to'],
            value=unlock_tx_object['value'],
            data=unlock_tx_object['data'])
        unlock_tx_unsigned_hash = sha3(
            rlp.encode(unlock_tx_unsigned_object,
                       transactions.UnsignedTransaction))

        self.verifier_contract.reveal(
            commit_block_number,  # uint32 _commitBlockNumber,
            _unlockExtraData,  # bytes _commitData,
            UNLOCK_AMOUNT,  # uint256 _unlockAmount,
            rec_bin(witness),  # bytes32 _witness,
            OURGASPRICE,  # uint256 _unlockGasPrice,
            OURGASLIMIT,  # uint256 _unlockGasLimit,
            unlock_tx_unsigned_hash,  # bytes32 _unlockTXHash,
            commit_proof_blob,  # bytes _proofBlob
            sender=ALICE_PRIVATE_KEY)
        log.info("Reveal TX Gas Used HeadState {}".format(
            self.chain.head_state.gas_used))
        reveal_gas = int(self.chain.head_state.gas_used)

        self.chain.mine(1)

        ##
        ## CHECK STATE AFTER REVEAL TX
        ##
        session_data = self.verifier_contract.getCommitState(rec_bin(commit))
        self.assertListEqual(
            session_data, [True, True, UNLOCK_AMOUNT, SPAM_AMOUNT],
            "After the Reveal, the state should report both revealed and unlocked."
        )
        finished_bool = self.verifier_contract.finished(rec_bin(commit))
        self.assertTrue(
            finished_bool,
            "The contract was unlocked first and then revealed, it should be finished"
        )


if __name__ == "__main__":
    unittest.main()
