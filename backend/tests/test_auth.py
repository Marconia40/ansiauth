def test_login_success(unauth_client):
    response = unauth_client.post("/api/v1/auth/login", data={"username": "admin", "password": "admin123"})

    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_login_invalid_credentials(unauth_client):
    response = unauth_client.post("/api/v1/auth/login", data={"username": "admin", "password": "wrongpass"})

    assert response.status_code == 401


def test_access_without_token(unauth_client):
    response = unauth_client.get("/api/v1/vlans/")

    assert response.status_code == 401


def test_access_with_invalid_token(unauth_client):
    response = unauth_client.get(
        "/api/v1/vlans/",
        headers={"Authorization": "Bearer this.is.not.a.valid.token"},
    )

    assert response.status_code == 401


def test_observer_cannot_create_vlan(observer_client):
    payload = {"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]}

    response = observer_client.post("/api/v1/vlans/", json=payload)

    assert response.status_code == 403


def test_operator_can_create_vlan(operator_client):
    payload = {"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]}

    response = operator_client.post("/api/v1/vlans/", json=payload)

    assert response.status_code == 200


def test_admin_can_delete_vlan(admin_client):
    response = admin_client.delete("/api/v1/vlans/10?device=mock_device")

    assert response.status_code == 200


def test_operator_cannot_delete_vlan(operator_client):
    response = operator_client.delete("/api/v1/vlans/10?device=mock_device")

    assert response.status_code == 403


def test_login_then_use_token(unauth_client):
    login_response = unauth_client.post(
        "/api/v1/auth/login", data={"username": "operator", "password": "operator123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]

    vlan_response = unauth_client.get(
        "/api/v1/vlans/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert vlan_response.status_code == 200
    assert isinstance(vlan_response.json()["data"], list)
