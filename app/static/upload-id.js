(function initializeUploadId(root) {
  let fallbackCounter = 0;

  function createFromRandomValues(cryptoApi) {
    const bytes = new Uint8Array(16);
    cryptoApi.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;

    const hex = Array.from(bytes, (byte) =>
      byte.toString(16).padStart(2, "0"),
    );
    return [
      hex.slice(0, 4).join(""),
      hex.slice(4, 6).join(""),
      hex.slice(6, 8).join(""),
      hex.slice(8, 10).join(""),
      hex.slice(10).join(""),
    ].join("-");
  }

  function createUploadId() {
    const cryptoApi = root.crypto;
    if (cryptoApi && typeof cryptoApi.randomUUID === "function") {
      return cryptoApi.randomUUID();
    }
    if (cryptoApi && typeof cryptoApi.getRandomValues === "function") {
      return createFromRandomValues(cryptoApi);
    }

    // The key only prevents duplicate task creation; it is not a credential.
    fallbackCounter += 1;
    const timestamp = Date.now().toString(36);
    const sequence = fallbackCounter.toString(36);
    const random = Math.random()
      .toString(36)
      .slice(2)
      .padEnd(12, "0")
      .slice(0, 12);
    return `upload-${timestamp}-${sequence}-${random}`;
  }

  root.WhisperUploadId = Object.freeze({ create: createUploadId });
})(typeof globalThis === "object" ? globalThis : window);
