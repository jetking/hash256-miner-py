import "dotenv/config";
import { Worker, isMainThread, parentPort, workerData } from "node:worker_threads";
import { randomBytes } from "node:crypto";
import { ethers } from "ethers";
import { keccak_256 } from "@noble/hashes/sha3.js";

const CONTRACT = "0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc";
const ABI = [
    "function getChallenge(address miner) view returns (bytes32)",
    "function miningState() view returns (uint256 era,uint256 reward,uint256 difficulty,uint256 minted,uint256 remaining,uint256 epoch,uint256 epochBlocksLeft)",
    "function mine(uint256 nonce)"
];

function hexToBytes32(hex) {
    return Uint8Array.from(Buffer.from(hex.replace(/^0x/, ""), "hex"));
}

function uint256be(n) {
    const out = new Uint8Array(32);
    let x = BigInt(n);
    for (let i = 31; i >= 0; i--) {
        out[i] = Number(x & 0xffn);
        x >>= 8n;
    }
    return out;
}

function hashChallengeNonce(challengeBytes, nonce) {
    const input = new Uint8Array(64);
    input.set(challengeBytes, 0);
    input.set(uint256be(nonce), 32);
    return keccak_256(input);
}

function bytesToBigInt(bytes) {
    let x = 0n;
    for (const b of bytes) x = (x << 8n) | BigInt(b);
    return x;
}

if (!isMainThread) {
    const challengeBytes = hexToBytes32(workerData.challenge);
    const target = BigInt(workerData.target);
    let nonce = BigInt(workerData.start);
    const stride = BigInt(workerData.stride);

    let checked = 0n;
    let last = Date.now();

    while (true) {
        const h = hashChallengeNonce(challengeBytes, nonce);
        if (bytesToBigInt(h) < target) {
            parentPort.postMessage({ type: "found", nonce: nonce.toString(), hash: Buffer.from(h).toString("hex") });
            break;
        }

        nonce += stride;
        checked++;

        if (checked % 50000n === 0n) {
            const now = Date.now();
            if (now - last > 1000) {
                parentPort.postMessage({ type: "rate", hashes: Number(checked) });
                checked = 0n;
                last = now;
            }
        }
    }
} else {
    const { RPC_URL, PRIVATE_KEY } = process.env;
    const WORKERS = Number(process.env.WORKERS || "4");

    if (!RPC_URL || !PRIVATE_KEY) {
        console.error("请在 .env 中设置 RPC_URL 和 PRIVATE_KEY");
        process.exit(1);
    }

    const provider = new ethers.JsonRpcProvider(RPC_URL);
    const wallet = new ethers.Wallet(PRIVATE_KEY, provider);
    const contract = new ethers.Contract(CONTRACT, ABI, wallet);

    console.log("miner:", wallet.address);
    console.log("contract:", CONTRACT);

    async function mineRound() {
        const state = await contract.miningState();
        const challenge = await contract.getChallenge(wallet.address);

        console.log(
            `era=${state.era} reward=${ethers.formatUnits(state.reward, 18)} HASH target=${state.difficulty.toString()} epoch=${state.epoch}`
        );
        console.log("challenge:", challenge);

        const workers = [];
        let total = 0;

        return await new Promise((resolve, reject) => {
            let done = false;

            for (let i = 0; i < WORKERS; i++) {
                const start = BigInt("0x" + randomBytes(16).toString("hex")) + BigInt(i);
                const w = new Worker(new URL(import.meta.url), {
                    workerData: {
                        challenge,
                        target: state.difficulty.toString(),
                        start: start.toString(),
                        stride: WORKERS.toString()
                    }
                });

                workers.push(w);

                w.on("message", async (msg) => {
                    if (msg.type === "rate") {
                        total += msg.hashes;
                        process.stdout.write(`\rhashes checked ~= ${total.toLocaleString()}`);
                    }

                    if (msg.type === "found" && !done) {
                        done = true;
                        console.log(`\nFOUND nonce=${msg.nonce} hash=0x${msg.hash}`);

                        for (const x of workers) x.terminate();

                        try {
                            const tx = await contract.mine(msg.nonce, { gasLimit: 250000 });
                            console.log("submitted:", tx.hash);
                            const receipt = await tx.wait();
                            console.log("confirmed block:", receipt.blockNumber);
                            resolve();
                        } catch (e) {
                            console.error("submit failed:", e.shortMessage || e.message);
                            resolve();
                        }
                    }
                });

                w.on("error", reject);
            }

            setTimeout(() => {
                if (!done) {
                    console.log("\nrefreshing challenge/epoch...");
                    for (const x of workers) x.terminate();
                    resolve();
                }
            }, 60_000);
        });
    }

    while (true) {
        await mineRound();
    }
}