"""Smoke test delle pagine pubbliche e delle protezioni sugli endpoint.

Sostituiscono i test precedenti, che chiamavano un endpoint `/api/examples`
mai esistito in questo progetto.
"""
import pytest


class TestPaginePubbliche:
    @pytest.mark.parametrize("path", ["/", "/consultants", "/login", "/register",
                                      "/about", "/come-funziona", "/faq", "/contact",
                                      "/privacy", "/terms"])
    def test_rispondono_200(self, client, path):
        assert client.get(path).status_code == 200


class TestProtezioneEndpoint:
    """Gli endpoint che toccano dati altrui non devono rispondere a chi non è autenticato."""

    @pytest.mark.parametrize("path", [
        "/api/notifications",
        "/api/notifications/unread/count",
        "/api/conversations",
        "/api/booking/my-bookings",
        "/api/booking/upcoming",
        "/api/profile/liked-questions",
    ])
    def test_get_senza_login(self, client, path):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code in (401, 302, 403), f"{path} ha risposto {resp.status_code}"

    def test_profilo_reindirizza_al_login(self, client):
        resp = client.get("/profile", follow_redirects=False)
        assert resp.status_code in (302, 307, 401)

    def test_admin_reindirizza_al_login(self, client):
        resp = client.get("/admin/", follow_redirects=False)
        assert resp.status_code in (302, 307, 401, 403)


class TestCsrf:
    def test_post_senza_token_viene_respinto(self, client):
        # Client "pulito", senza header CSRF
        resp = client.post("/api/booking/1/join", headers={"X-CSRF-Token": ""})
        assert resp.status_code == 403
        assert "CSRF" in resp.text

    def test_il_token_e_esposto_nella_pagina(self, client):
        html = client.get("/login").text
        assert 'name="csrf-token"' in html

    def test_post_con_token_supera_il_middleware(self, csrf_client):
        # Non deve piu' essere 403 per CSRF: senza login ci si aspetta 401
        resp = csrf_client.post("/api/booking/999999/join")
        assert resp.status_code != 403
