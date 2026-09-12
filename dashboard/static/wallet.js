"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const button = document.getElementById("wallet-sign");
  if (!button) return;
  button.addEventListener("click", async () => {
    const status = document.getElementById("wallet-status");
    try {
      if (!window.ethereum) throw new Error("Nie znaleziono Rabby ani MetaMask w tej przeglądarce.");
      const expected = document.getElementById("wallet-address").textContent.trim().toLowerCase();
      const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
      const account = String(accounts[0] || "").toLowerCase();
      if (account !== expected) throw new Error(`Wybrano ${account}; wymagany jest ${expected}.`);
      const message = document.getElementById("wallet-preimage").textContent;
      const bytes = new TextEncoder().encode(message);
      const hex = "0x" + Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
      status.textContent = "Sprawdź wiadomość w portfelu i zatwierdź podpis.";
      const signature = await window.ethereum.request({ method: "personal_sign", params: [hex, account] });
      document.getElementById("wallet-signature").value = signature;
      document.getElementById("wallet-bind-form").submit();
    } catch (error) {
      status.textContent = error && error.message ? error.message : String(error);
    }
  });
});
