import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";

if (!process.argv[2]) {
  throw new Error("Usage: node tests/data/chainsig_reference.mjs <npm-prefix> [--check]");
}

const prefix = resolve(process.argv[2]);
const referenceRequire = createRequire(resolve(prefix, "package.json"));
const fixture = JSON.parse(
  readFileSync(new URL("./chainsig_reference.json", import.meta.url), "utf8"),
);

for (const [name, expected] of Object.entries(fixture.provenance.versions)) {
  const installed = JSON.parse(
    readFileSync(resolve(prefix, "node_modules", name, "package.json"), "utf8"),
  );
  assert.equal(installed.version, expected, `${name} version`);
}

const { chainAdapters, utils } = referenceRequire("chainsig.js");
const bitcoin = referenceRequire("bitcoinjs-lib");
const sha3 = referenceRequire("js-sha3");
const base58 = referenceRequire("bs58").default;
const { getAddress, keccak256 } = referenceRequire("viem");
const cryptography = utils.cryptography;
const vectors = [];

for (const input of fixture.inputs) {
  const observation = fixture.root_observations.find(
    (root) => root.id === input.root_id,
  );
  assert.ok(observation, input.root_id);
  const secpRoot = observation.public_keys[String(input.secp256k1_domain_id)];
  const edRoot = observation.public_keys[String(input.ed25519_domain_id)];
  const childSecp = cryptography.deriveChildPublicKey(
    cryptography.najToUncompressedPubKeySEC1(secpRoot),
    input.predecessor,
    input.path,
  );
  const childEd = cryptography.deriveChildPublicKeyEd25519(
    edRoot,
    input.predecessor,
    input.path,
  );
  const contract = {
    async getDerivedPublicKey({ predecessor, path, IsEd25519 }) {
      assert.equal(predecessor, input.predecessor);
      assert.equal(path, input.path);
      return IsEd25519 ? childEd : childSecp;
    },
  };
  const evm = await new chainAdapters.evm.EVM({
    contract,
    publicClient: {},
  }).deriveAddressAndPublicKey(input.predecessor, input.path);
  const solana = await new chainAdapters.solana.Solana({
    contract,
    solanaConnection: {},
  }).deriveAddressAndPublicKey(input.predecessor, input.path);
  const compressed = cryptography.compressPubKey(childSecp);
  const publicKey = Buffer.from(compressed, "hex");
  const bitcoinAddresses = {};

  for (const [networkId, network] of [
    ["mainnet", bitcoin.networks.bitcoin],
    ["testnet", bitcoin.networks.testnet],
    ["regtest", bitcoin.networks.regtest],
  ]) {
    const adapter = new chainAdapters.btc.Bitcoin({
      network: networkId,
      contract,
      btcRpcAdapter: {},
    });
    const native = await adapter.deriveAddressAndPublicKey(
      input.predecessor,
      input.path,
    );
    const witness = bitcoin.payments.p2wpkh({ pubkey: publicKey, network });
    assert.equal(native.publicKey, compressed);
    assert.equal(native.address, witness.address);
    bitcoinAddresses[networkId] = {
      p2wpkh: native.address,
      p2pkh: bitcoin.payments.p2pkh({ pubkey: publicKey, network }).address,
      p2sh_p2wpkh: bitcoin.payments.p2sh({ redeem: witness, network }).address,
    };
  }

  assert.equal(evm.publicKey, childSecp);
  assert.equal(solana.address, childEd.slice("ed25519:".length));
  const tweakMessage =
    "near-mpc-recovery v0.1.0 epsilon derivation:" +
    input.predecessor +
    "," +
    input.path;
  vectors.push({
    name: input.name,
    root_id: input.root_id,
    predecessor: input.predecessor,
    path: input.path,
    tweak_hex: sha3.sha3_256(tweakMessage),
    secp256k1: {
      domain_id: input.secp256k1_domain_id,
      public_key_sec1_hex: childSecp,
      public_key_naj:
        "secp256k1:" + base58.encode(Buffer.from(childSecp.slice(2), "hex")),
      public_key_compressed_hex: compressed,
      hash160_hex: bitcoin.crypto.hash160(publicKey).toString("hex"),
      ethereum: evm.address,
      bitcoin: bitcoinAddresses,
    },
    ed25519: {
      domain_id: input.ed25519_domain_id,
      public_key_naj: childEd,
      public_key_hex: Buffer.from(
        base58.decode(childEd.slice("ed25519:".length)),
      ).toString("hex"),
      solana: solana.address,
    },
  });
}

for (const known of fixture.captured_reference_answers) {
  const vector = vectors.find((row) => row.name === known.vector);
  assert.equal(vector.secp256k1.public_key_naj, known.secp256k1);
  assert.equal(vector.ed25519.public_key_naj, known.ed25519);
}

for (const observation of fixture.contract_derived_observations) {
  const vector = vectors.find((row) => row.name === observation.vector);
  assert.equal(vector.root_id, observation.root_id);
  assert.equal(vector.predecessor, observation.args.predecessor);
  assert.equal(vector.path, observation.args.path);
  for (const curve of ["secp256k1", "ed25519"]) {
    assert.equal(
      vector[curve].public_key_naj,
      observation.public_keys[String(vector[curve].domain_id)],
    );
  }
}

const keccakVectors = [0, 3, 135, 136, 137, 272].map((length) => {
  const input =
    length === 3
      ? Buffer.from("abc", "utf8")
      : Buffer.from(Array.from({ length }, (_, index) => index % 256));
  const digest = sha3.keccak_256(input);
  assert.equal(keccak256(input), "0x" + digest);
  return { length, input_hex: input.toString("hex"), digest_hex: digest };
});

const ec = new (referenceRequire("elliptic").ec)("secp256k1");
const generatorSEC1 = ec.g.encode("hex", false);
const generatorCompressed = ec.g.encode("hex", true);
const generatorPubkey = Buffer.from(generatorCompressed, "hex");
const generatorAddresses = {};
for (const [networkId, network] of [
  ["mainnet", bitcoin.networks.bitcoin],
  ["testnet", bitcoin.networks.testnet],
  ["regtest", bitcoin.networks.regtest],
]) {
  const witness = bitcoin.payments.p2wpkh({ pubkey: generatorPubkey, network });
  generatorAddresses[networkId] = {
    p2wpkh: witness.address,
    p2pkh: bitcoin.payments.p2pkh({ pubkey: generatorPubkey, network }).address,
    p2sh_p2wpkh: bitcoin.payments.p2sh({ redeem: witness, network }).address,
  };
}
const primitives = {
  generator: {
    public_key_sec1_hex: generatorSEC1,
    public_key_compressed_hex: generatorCompressed,
    two_g_compressed_hex: ec.g.mul(2).encode("hex", true),
    public_key_naj:
      "secp256k1:" +
      base58.encode(Buffer.from(generatorSEC1.slice(2), "hex")),
    ethereum: getAddress(
      "0x" + keccak256(Buffer.from(generatorSEC1.slice(2), "hex")).slice(-40),
    ),
    bitcoin: generatorAddresses,
  },
};

if (process.argv.includes("--check")) {
  assert.deepEqual(vectors, fixture.vectors);
  assert.deepEqual(keccakVectors, fixture.keccak_vectors);
  assert.deepEqual(primitives, fixture.primitives);
  console.log(
    `${vectors.length} derivation vectors and ${keccakVectors.length} Keccak vectors match`,
  );
} else {
  console.log(
    JSON.stringify(
      { ...fixture, vectors, keccak_vectors: keccakVectors, primitives },
      null,
      2,
    ),
  );
}
