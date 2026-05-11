/*
 * hash256.org GPU miner kernel
 * --------------------------------------------------------------------------
 * Implements the PoW described in the HASH whitepaper:
 *
 *   challenge = keccak256(chainId ‖ contract ‖ miner ‖ epoch)
 *   valid iff keccak256(challenge ‖ nonce) < currentDifficulty
 *
 * This kernel only computes the inner hash. The host code precomputes
 * `challenge` (32 bytes) and the target (32 bytes), and feeds them to each
 * work item with a unique nonce. A work item that finds nonce satisfying
 * keccak256(challenge ‖ nonce_be32) < target writes the nonce to result[0]
 * and sets result[1] = 1.
 *
 * Layout of the 64-byte preimage that gets keccak'd:
 *     bytes 0..31  : challenge   (precomputed by host)
 *     bytes 32..63 : nonce       (uint256, big-endian)
 *
 * For performance we keep the upper 28 bytes of nonce at zero on the GPU and
 * only vary the low 32 bits. The host bumps a base_nonce every iteration so
 * the full 256-bit space stays reachable.
 *
 * Difficulty comparison is done as a 256-bit big-endian compare.
 * --------------------------------------------------------------------------
 */

// ------------------------------ Keccak-f[1600] -----------------------------
// Standard round constants
__constant ulong RC[24] = {
    0x0000000000000001UL, 0x0000000000008082UL, 0x800000000000808aUL,
    0x8000000080008000UL, 0x000000000000808bUL, 0x0000000080000001UL,
    0x8000000080008081UL, 0x8000000000008009UL, 0x000000000000008aUL,
    0x0000000000000088UL, 0x0000000080008009UL, 0x000000008000000aUL,
    0x000000008000808bUL, 0x800000000000008bUL, 0x8000000000008089UL,
    0x8000000000008003UL, 0x8000000000008002UL, 0x8000000000000080UL,
    0x000000000000800aUL, 0x800000008000000aUL, 0x8000000080008081UL,
    0x8000000000008080UL, 0x0000000080000001UL, 0x8000000080008008UL
};

static inline ulong rotl64(ulong x, uint n) {
    return (x << n) | (x >> (64u - n));
}

// NVIDIA GPUs can lower a three-input xor to LOP3, reducing the instruction
// count in Theta. Keep non-NVIDIA devices on the original two-step form below.
static inline ulong lop3_xor3(ulong a, ulong b, ulong c) {
    return a ^ b ^ c;
}

// In-place Keccak-f[1600] permutation on a 25-lane state.
static void keccak_f1600(ulong st[25]) {
    ulong t, bc0, bc1, bc2, bc3, bc4;
    for (int r = 0; r < 24; r++) {
        // Theta
        bc0 = st[0] ^ st[5] ^ st[10] ^ st[15] ^ st[20];
        bc1 = st[1] ^ st[6] ^ st[11] ^ st[16] ^ st[21];
        bc2 = st[2] ^ st[7] ^ st[12] ^ st[17] ^ st[22];
        bc3 = st[3] ^ st[8] ^ st[13] ^ st[18] ^ st[23];
        bc4 = st[4] ^ st[9] ^ st[14] ^ st[19] ^ st[24];

#if defined(__NV_CL_C_VERSION)
        // Apply each column delta as a single three-input xor so NVIDIA's
        // backend can use LOP3 for st[i] ^ c[x-1] ^ ROT(c[x+1], 1).
        ulong rc0 = rotl64(bc1, 1);
        ulong rc1 = rotl64(bc2, 1);
        ulong rc2 = rotl64(bc3, 1);
        ulong rc3 = rotl64(bc4, 1);
        ulong rc4 = rotl64(bc0, 1);
        st[ 0] = lop3_xor3(st[ 0], bc4, rc0); st[ 5] = lop3_xor3(st[ 5], bc4, rc0);
        st[10] = lop3_xor3(st[10], bc4, rc0); st[15] = lop3_xor3(st[15], bc4, rc0);
        st[20] = lop3_xor3(st[20], bc4, rc0);

        st[ 1] = lop3_xor3(st[ 1], bc0, rc1); st[ 6] = lop3_xor3(st[ 6], bc0, rc1);
        st[11] = lop3_xor3(st[11], bc0, rc1); st[16] = lop3_xor3(st[16], bc0, rc1);
        st[21] = lop3_xor3(st[21], bc0, rc1);

        st[ 2] = lop3_xor3(st[ 2], bc1, rc2); st[ 7] = lop3_xor3(st[ 7], bc1, rc2);
        st[12] = lop3_xor3(st[12], bc1, rc2); st[17] = lop3_xor3(st[17], bc1, rc2);
        st[22] = lop3_xor3(st[22], bc1, rc2);

        st[ 3] = lop3_xor3(st[ 3], bc2, rc3); st[ 8] = lop3_xor3(st[ 8], bc2, rc3);
        st[13] = lop3_xor3(st[13], bc2, rc3); st[18] = lop3_xor3(st[18], bc2, rc3);
        st[23] = lop3_xor3(st[23], bc2, rc3);

        st[ 4] = lop3_xor3(st[ 4], bc3, rc4); st[ 9] = lop3_xor3(st[ 9], bc3, rc4);
        st[14] = lop3_xor3(st[14], bc3, rc4); st[19] = lop3_xor3(st[19], bc3, rc4);
        st[24] = lop3_xor3(st[24], bc3, rc4);
#else
        t = bc4 ^ rotl64(bc1, 1);
        st[0] ^= t; st[5] ^= t; st[10] ^= t; st[15] ^= t; st[20] ^= t;
        t = bc0 ^ rotl64(bc2, 1);
        st[1] ^= t; st[6] ^= t; st[11] ^= t; st[16] ^= t; st[21] ^= t;
        t = bc1 ^ rotl64(bc3, 1);
        st[2] ^= t; st[7] ^= t; st[12] ^= t; st[17] ^= t; st[22] ^= t;
        t = bc2 ^ rotl64(bc4, 1);
        st[3] ^= t; st[8] ^= t; st[13] ^= t; st[18] ^= t; st[23] ^= t;
        t = bc3 ^ rotl64(bc0, 1);
        st[4] ^= t; st[9] ^= t; st[14] ^= t; st[19] ^= t; st[24] ^= t;
#endif

        // Rho + Pi
        ulong tmp = st[1];
        ulong v;
        v = st[10]; st[10] = rotl64(tmp,  1); tmp = v;
        v = st[ 7]; st[ 7] = rotl64(tmp,  3); tmp = v;
        v = st[11]; st[11] = rotl64(tmp,  6); tmp = v;
        v = st[17]; st[17] = rotl64(tmp, 10); tmp = v;
        v = st[18]; st[18] = rotl64(tmp, 15); tmp = v;
        v = st[ 3]; st[ 3] = rotl64(tmp, 21); tmp = v;
        v = st[ 5]; st[ 5] = rotl64(tmp, 28); tmp = v;
        v = st[16]; st[16] = rotl64(tmp, 36); tmp = v;
        v = st[ 8]; st[ 8] = rotl64(tmp, 45); tmp = v;
        v = st[21]; st[21] = rotl64(tmp, 55); tmp = v;
        v = st[24]; st[24] = rotl64(tmp,  2); tmp = v;
        v = st[ 4]; st[ 4] = rotl64(tmp, 14); tmp = v;
        v = st[15]; st[15] = rotl64(tmp, 27); tmp = v;
        v = st[23]; st[23] = rotl64(tmp, 41); tmp = v;
        v = st[19]; st[19] = rotl64(tmp, 56); tmp = v;
        v = st[13]; st[13] = rotl64(tmp,  8); tmp = v;
        v = st[12]; st[12] = rotl64(tmp, 25); tmp = v;
        v = st[ 2]; st[ 2] = rotl64(tmp, 43); tmp = v;
        v = st[20]; st[20] = rotl64(tmp, 62); tmp = v;
        v = st[14]; st[14] = rotl64(tmp, 18); tmp = v;
        v = st[22]; st[22] = rotl64(tmp, 39); tmp = v;
        v = st[ 9]; st[ 9] = rotl64(tmp, 61); tmp = v;
        v = st[ 6]; st[ 6] = rotl64(tmp, 20); tmp = v;
                    st[ 1] = rotl64(tmp, 44);

        // Chi
        for (int j = 0; j < 25; j += 5) {
            ulong a0 = st[j+0], a1 = st[j+1], a2 = st[j+2], a3 = st[j+3], a4 = st[j+4];
            st[j+0] = a0 ^ ((~a1) & a2);
            st[j+1] = a1 ^ ((~a2) & a3);
            st[j+2] = a2 ^ ((~a3) & a4);
            st[j+3] = a3 ^ ((~a4) & a0);
            st[j+4] = a4 ^ ((~a0) & a1);
        }

        // Iota
        st[0] ^= RC[r];
    }
}

// --------------------------------------------------------------------------
// Reverse the byte order of a 64-bit word. Used for big-endian compare and
// for placing the nonce in big-endian order inside the preimage.
static inline ulong bswap64(ulong x) {
    x = ((x & 0x00000000FFFFFFFFUL) << 32) | ((x & 0xFFFFFFFF00000000UL) >> 32);
    x = ((x & 0x0000FFFF0000FFFFUL) << 16) | ((x & 0xFFFF0000FFFF0000UL) >> 16);
    x = ((x & 0x00FF00FF00FF00FFUL) <<  8) | ((x & 0xFF00FF00FF00FF00UL) >>  8);
    return x;
}

// Compare a little-endian Keccak state (first 4 lanes = the 32-byte digest)
// against a big-endian 32-byte target stored as 4 ulongs in big-endian order.
// Returns 1 if digest < target.
static int digest_lt_target(const ulong st[25], __constant const ulong *target_be) {
    // digest bytes 0..31 == st[0], st[1], st[2], st[3] in little-endian byte order.
    // Treating the digest as a big-endian 256-bit number means byte 0 of the
    // hash is the most-significant byte. So we compare bswap64(st[i]) against
    // target_be[i] from i=0 (most significant) downward.
    for (int i = 0; i < 4; i++) {
        ulong d = bswap64(st[i]);
        ulong t = target_be[i];
        if (d < t) return 1;
        if (d > t) return 0;
    }
    return 0; // equal counts as not-less-than
}

// --------------------------------------------------------------------------
// The mining kernel.
//
//   challenge_be : 32-byte challenge as 4x ulong, each lane in *little-endian*
//                  word order matching how the preimage will be absorbed.
//                  In practice the host writes 4 little-endian u64s read from
//                  the raw 32 challenge bytes.
//   target_be    : 32-byte target stored as 4x ulong big-endian (so [0] holds
//                  the most-significant 8 bytes).
//   base_nonce_hi: high 64 bits of the 96-bit nonce-low base (we vary 32 bits
//                  per kernel launch and the host bumps the upper bits).
//   base_nonce_md: middle 64 bits.
//   base_nonce_lo_hi32: high 32 bits of the low 64 bits; the work-item ID
//                  fills the lower 32 bits.
//   result       : output buffer. result[0] = found nonce low 64 bits,
//                  result[1] = found nonce middle 64 bits,
//                  result[2] = found nonce high 64 bits,
//                  result[3] = 1 if any work item found a solution.
//
// The 256-bit nonce laid out in the preimage (bytes 32..63) is big-endian.
// To match the Solidity behavior we treat the nonce as uint256 big-endian.
// --------------------------------------------------------------------------
__kernel void mine(
    __constant const ulong *challenge_le,   // 4 lanes
    __constant const ulong *target_be,      // 4 lanes
    const ulong nonce_word0_be,             // bytes 32..39 of preimage (BE)
    const ulong nonce_word1_be,             // bytes 40..47
    const ulong nonce_word2_be,             // bytes 48..55
    const ulong nonce_word3_base_be,        // bytes 56..63 base (lower 32 bits
                                            //   replaced by gid)
    __global ulong *result
) {
    uint gid = get_global_id(0);

    // Compose the final 64-bit BE word that holds the varying nonce tail.
    // nonce_word3_base_be already has the lower 32 bits zeroed by the host.
    // The big-endian nonce tail = (high32_base) || (gid as BE 32 bits).
    // Stored as a 64-bit LE ulong inside the state, we need bswap.
    ulong word3_be = nonce_word3_base_be | (ulong)gid;

    // Build the 1600-bit state. Rate r = 1088 bits = 136 bytes. Our message
    // is 64 bytes — fits in one block. Padding rule for Keccak-256 (NIST
    // SHA-3 uses 0x06, but Ethereum's keccak uses original Keccak padding
    // 0x01 ... 0x80).
    ulong st[25];
    #pragma unroll
    for (int i = 0; i < 25; i++) st[i] = 0;

    // Absorb challenge bytes 0..31 → lanes 0..3 (little-endian word order).
    st[0] = challenge_le[0];
    st[1] = challenge_le[1];
    st[2] = challenge_le[2];
    st[3] = challenge_le[3];

    // Absorb nonce bytes 32..63 → lanes 4..7. The nonce is conceptually
    // big-endian in the preimage; bswap converts each 8-byte BE chunk to
    // the little-endian ulong representation Keccak expects.
    st[4] = bswap64(nonce_word0_be);
    st[5] = bswap64(nonce_word1_be);
    st[6] = bswap64(nonce_word2_be);
    st[7] = bswap64(word3_be);

    // Keccak (original, not SHA-3) padding: append 0x01 at byte 64, then
    // pad with zeros, then OR 0x80 into the last byte of the rate (byte 135).
    // Byte 64 lives in lane 8 at byte position 0 → low byte of st[8].
    // Byte 135 lives in lane 16 at byte position 7 → top byte of st[16].
    st[8]  ^= 0x0000000000000001UL;
    st[16] ^= 0x8000000000000000UL;

    keccak_f1600(st);

    // Now lanes 0..3 hold the 256-bit digest. Compare against target.
    if (digest_lt_target(st, target_be)) {
        // Race-safe write: atomic CAS so only the first finder wins per launch.
        if (atomic_cmpxchg((__global int*)&result[3], 0, 1) == 0) {
            result[0] = word3_be;          // lowest 64 BE bits of nonce
            result[1] = nonce_word2_be;
            result[2] = nonce_word1_be;
            // result[3] already set to 1 by CAS
            // We also need the very top 64 BE bits.
            // Pack into result[4..]:
            result[4] = nonce_word0_be;
        }
    }
}
