/**
 * OpenSSL-compatible AES-256-CBC encryption/decryption using Web Crypto API.
 *
 * File format (same as `openssl enc -aes-256-cbc -salt -pbkdf2`):
 *   Bytes  0-7:  "Salted__"  (magic header)
 *   Bytes  8-15: 8-byte random salt
 *   Bytes 16+:   AES-256-CBC ciphertext (PKCS7 padded)
 *
 * Two key flavors:
 *   v2:     password = sha256("ICSv2:" + stuid + ":" + uispsw)  (hex)
 *           PBKDF2 iterations: 100000
 *   legacy: password = stuid + uispsw + dashscope + smtp        (concat)
 *           PBKDF2 iterations: 10000
 *
 * Derivation:
 *   PBKDF2-HMAC-SHA256(password, salt, iterations, dkLen=48)
 *   -> first 32 bytes = AES key, last 16 bytes = IV
 */

window.ICS = window.ICS || {};

var MAGIC = new TextEncoder().encode("Salted__");
var NEW_ITERATIONS = 100000;
var LEGACY_ITERATIONS = 10000;

function _checkWebCrypto() {
  if (!window.crypto || !window.crypto.subtle) {
    throw new Error(
      "Web Crypto API is not available. Please access this page via HTTPS (GitHub Pages) or http://localhost. " +
      "Current protocol: " + location.protocol + " host: " + location.host
    );
  }
}

async function _sha256Hex(text) {
  _checkWebCrypto();
  var bytes = new TextEncoder().encode(text);
  var digest = await window.crypto.subtle.digest("SHA-256", bytes);
  var arr = new Uint8Array(digest);
  return Array.from(arr).map(function (b) {
    return b.toString(16).padStart(2, "0");
  }).join("");
}

async function _deriveKeyAndIV(password, salt, iterations) {
  _checkWebCrypto();
  var enc = new TextEncoder();
  var baseKey = await window.crypto.subtle.importKey(
    "raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]
  );
  var bits = await window.crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: salt, iterations: iterations, hash: "SHA-256" },
    baseKey, 48 * 8
  );
  var key = await window.crypto.subtle.importKey(
    "raw", bits.slice(0, 32), { name: "AES-CBC" }, false, ["encrypt", "decrypt"]
  );
  return { key: key, iv: new Uint8Array(bits.slice(32, 48)) };
}

async function _icsDecrypt(encryptedBytes, password, iterations) {
  iterations = iterations || NEW_ITERATIONS;
  var headerStr = new TextDecoder().decode(encryptedBytes.slice(0, 8));
  if (headerStr !== "Salted__") {
    throw new Error("Invalid file: missing OpenSSL 'Salted__' header");
  }
  var salt = encryptedBytes.slice(8, 16);
  var ciphertext = encryptedBytes.slice(16);
  var derived = await _deriveKeyAndIV(password, salt, iterations);
  var plainBuffer = await window.crypto.subtle.decrypt(
    { name: "AES-CBC", iv: derived.iv }, derived.key, ciphertext
  );
  return new Uint8Array(plainBuffer);
}

async function _icsEncrypt(plainBytes, password, iterations) {
  iterations = iterations || NEW_ITERATIONS;
  _checkWebCrypto();
  var salt = window.crypto.getRandomValues(new Uint8Array(8));
  var derived = await _deriveKeyAndIV(password, salt, iterations);
  var cipherBuffer = await window.crypto.subtle.encrypt(
    { name: "AES-CBC", iv: derived.iv }, derived.key, plainBytes
  );
  var cipherBytes = new Uint8Array(cipherBuffer);
  var result = new Uint8Array(MAGIC.length + salt.length + cipherBytes.length);
  result.set(MAGIC, 0);
  result.set(salt, MAGIC.length);
  result.set(cipherBytes, MAGIC.length + salt.length);
  return result;
}

async function _icsBuildPasswordV2(secrets) {
  return await _sha256Hex("ICSv2:" + secrets.stuid + ":" + secrets.uispsw);
}

function _icsBuildPasswordLegacy(secrets) {
  return secrets.stuid + secrets.uispsw +
         (secrets.dashscope || "") + (secrets.smtp || "");
}

/* Plaintext sanity validators — mirrors src/crypto_box.py. AES-CBC + PKCS7
   has a ~1/256 chance of accepting a wrong key (the last byte happens to be
   0x01).  Pass one of these to decryptWithFallback so the wrong-key case
   gets rejected and the next key is tried instead of propagating garbage. */
function _icsIsSqlite(pt) {
  if (!pt || pt.length < 16) return false;
  var prefix = "SQLite format 3";
  for (var i = 0; i < prefix.length; i++) {
    if (pt[i] !== prefix.charCodeAt(i)) return false;
  }
  return pt[15] === 0;
}

function _icsIsGzip(pt) {
  return !!pt && pt.length >= 2 && pt[0] === 0x1F && pt[1] === 0x8B;
}

function _icsIsJsonObj(pt) {
  if (!pt) return false;
  for (var i = 0; i < pt.length; i++) {
    var c = pt[i];
    if (c === 0x20 || c === 0x09 || c === 0x0A || c === 0x0D) continue;
    return c === 0x7B || c === 0x5B; // '{' or '['
  }
  return false;
}

async function _icsDecryptWithFallback(encryptedBytes, secrets, validate) {
  try {
    var pwV2 = await _icsBuildPasswordV2(secrets);
    var ptV2 = await _icsDecrypt(encryptedBytes, pwV2, NEW_ITERATIONS);
    if (!validate || validate(ptV2)) {
      return { data: ptV2, version: "v2" };
    }
  } catch (e) {
    // fall through to legacy
  }
  var pwLegacy = _icsBuildPasswordLegacy(secrets);
  var ptLegacy = await _icsDecrypt(encryptedBytes, pwLegacy, LEGACY_ITERATIONS);
  if (validate && !validate(ptLegacy)) {
    throw new Error(
      "Decryption produced bytes that did not pass plaintext validation. " +
      "Wrong credentials?"
    );
  }
  return { data: ptLegacy, version: "legacy" };
}

/* ── V3: HKDF-based encryption (master key derived once) ───────────────
 *
 * V3 envelope:
 *   Bytes  0-7:  "ICSv3\x00\x00\x00"  (magic)
 *   Bytes  8-39: SHA-256(file_id)      (32 bytes, used as HKDF info)
 *   Bytes 40+:   AES-256-CBC ciphertext (PKCS7 padded)
 *
 * Key derivation:
 *   1. PBKDF2(password, fixed_salt, 100000) -> 32-byte master_key
 *   2. HKDF(master_key, info=bytes[8:40], salt="") -> 48 bytes
 *      first 32 = AES key, last 16 = IV
 */

var V3_MAGIC = "ICSv3\x00\x00\x00";
var V3_MAGIC_LEN = 8;
var V3_INFO_LEN = 32;
var V3_HEADER_LEN = V3_MAGIC_LEN + V3_INFO_LEN; // 40

async function _icsBuildPasswordV3(secrets) {
  return await _sha256Hex("ICSv3:" + secrets.stuid + ":" + secrets.uispsw);
}

async function _icsDeriveV3MasterKey(password, saltHex, iterations) {
  _checkWebCrypto();
  var salt = new Uint8Array(saltHex.match(/.{2}/g).map(function(b) { return parseInt(b, 16); }));
  var enc = new TextEncoder();
  var baseKey = await window.crypto.subtle.importKey(
    "raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]
  );
  var masterBits = await window.crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: salt, iterations: iterations, hash: "SHA-256" },
    baseKey, 256
  );
  // Import the 32-byte master key as HKDF key material
  return await window.crypto.subtle.importKey(
    "raw", masterBits, "HKDF", false, ["deriveBits"]
  );
}

async function _icsHkdfDecrypt(encryptedBytes, masterKey) {
  _checkWebCrypto();
  if (encryptedBytes.length < V3_HEADER_LEN + 16) {
    throw new Error("V3 blob too short");
  }
  // Verify magic
  var magic = new TextDecoder().decode(encryptedBytes.slice(0, V3_MAGIC_LEN));
  if (magic !== V3_MAGIC) {
    throw new Error("Not a V3 encrypted file (bad magic)");
  }
  // Extract info (file_id hash) and ciphertext
  var info = encryptedBytes.slice(V3_MAGIC_LEN, V3_HEADER_LEN);
  var ciphertext = encryptedBytes.slice(V3_HEADER_LEN);
  // HKDF derive 48 bytes (32 key + 16 IV)
  var derived = await window.crypto.subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0), info: info },
    masterKey, 384
  );
  var keyBytes = derived.slice(0, 32);
  var iv = new Uint8Array(derived.slice(32, 48));
  var aesKey = await window.crypto.subtle.importKey(
    "raw", keyBytes, { name: "AES-CBC" }, false, ["decrypt"]
  );
  var plainBuffer = await window.crypto.subtle.decrypt(
    { name: "AES-CBC", iv: iv }, aesKey, ciphertext
  );
  return new Uint8Array(plainBuffer);
}

window.ICS.crypto = {
  decrypt: _icsDecrypt,
  encrypt: _icsEncrypt,
  buildPassword: _icsBuildPasswordV2,
  buildPasswordV2: _icsBuildPasswordV2,
  buildPasswordV3: _icsBuildPasswordV3,
  buildPasswordLegacy: _icsBuildPasswordLegacy,
  decryptWithFallback: _icsDecryptWithFallback,
  deriveV3MasterKey: _icsDeriveV3MasterKey,
  hkdfDecrypt: _icsHkdfDecrypt,
  isSqlite: _icsIsSqlite,
  isGzip: _icsIsGzip,
  isJsonObj: _icsIsJsonObj,
  NEW_ITERATIONS: NEW_ITERATIONS,
  LEGACY_ITERATIONS: LEGACY_ITERATIONS,
};
