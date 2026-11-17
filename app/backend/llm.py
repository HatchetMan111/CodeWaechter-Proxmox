#!/usr/bin/env python3
"""
CodeWächter – Optionaler LLM-Client (OpenAI-kompatibel).

Die App ist voll funktionsfähig OHNE diesen Teil (deterministischer Plan).
Ist ein Endpoint konfiguriert (Ollama mit OpenAI-Adapter, ZCodeAI-Server,
llama.cpp-Server, vLLM … alles im LAN), werden Plan-Zusammenfassungen
frei formuliert. Fehler werden als Exception mit voller Meldungskette
an den Aufrufer weitergereicht – die UI zeigt sie vollständig an.
"""

import requests

HTTP_TIMEOUT = 60


def _headers(api_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def chat_completion(base_url: str, api_key: str, model: str,
                    messages: list[dict], timeout: int = HTTP_TIMEOUT) -> str:
    """Ein Chat-Completion an einen OpenAI-kompatiblen Endpoint senden.
    Liefert den Antworttext; bei Fehlernrequests die volle Fehlerkette
    (HTTP-Status + Body-Auszug) als RuntimeError."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": 0.2}
    try:
        resp = requests.post(url, headers=_headers(api_key), json=payload,
                             timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"LLM-Endpoint nicht erreichbar ({url}): {type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"LLM-Endpoint antwortete HTTP {resp.status_code}: {resp.text[:500]}"
        )
    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise RuntimeError(
            f"Unerwartetes LLM-Antwortformat: {str(exc)} | Body: {resp.text[:500]}"
        ) from exc


def test_connection(base_url: str, api_key: str, model: str) -> dict:
    """Verbindungstest aus den Einstellungen. Rückgabe: {'ok': bool, 'message': str}"""
    if not base_url or not model:
        return {"ok": False, "message": "Base-URL und Modell müssen gesetzt sein."}
    try:
        answer = chat_completion(
            base_url=base_url, api_key=api_key, model=model,
            messages=[{"role": "user", "content":
                       "Antworte mit genau einem Wort: OK"}],
            timeout=20,
        )
        return {"ok": True, "message": f"Verbindung OK – Antwort: {answer.strip()[:80]}"}
    except RuntimeError as exc:
        return {"ok": False, "message": str(exc)}
