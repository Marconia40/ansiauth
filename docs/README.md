# Network Automation API

## 📌 Descripción

Este proyecto implementa una API REST para la automatización de dispositivos de red multi-vendor.
El sistema abstrae la configuración específica de cada proveedor mediante una interfaz unificada y utiliza Ansible como motor de automatización.

Actualmente el proyecto se encuentra en etapa de desarrollo con ejecución **mockeada** (sin dispositivos reales).

---

## 🏗️ Arquitectura

El sistema sigue el siguiente flujo:

```
Usuario → API (FastAPI) → Validaciones → Job (async) → Servicio → (Mock / Ansible) → Resultado
```

Componentes principales:

* **API (FastAPI)**: expone endpoints REST
* **Validadores**: lógica de validación de entrada
* **Job System**: ejecución asíncrona de tareas
* **Servicios**: lógica de negocio (VLAN, etc.)
* **Ansible (futuro)**: ejecución real en dispositivos

---

## ⚙️ Requisitos

* Python 3.10+
* pip
* virtualenv (opcional pero recomendado)

---

## 🚀 Instalación

### 1. Clonar repositorio

```bash
git clone <repo_url>
cd ansiauth
```

### 2. Crear entorno virtual

```bash
python -m venv venv
source venv/bin/activate
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt
```

---

## ▶️ Ejecución

Levantar la API:


```bash
cd backend/
uvicorn app.main:app --reload
```
or
```bash
uvicorn backend.app.main:app --reload
```

La API estará disponible en:

```
http://127.0.0.1:8000
```

Swagger UI:

```
http://127.0.0.1:8000/docs
```

---

## 🧪 Pruebas (modo MOCK)

Actualmente las operaciones no impactan dispositivos reales. Antes de levantar la API ejecutar (export MOCK_MODE=true)

### Crear VLAN

**Endpoint:**

```
POST /api/v1/vlans/
```

**Ejemplo:**

```json
{
  "vlan_id": 10,
  "name": "TEST",
  "device": "mock_device"
}
```

**Respuesta:**

```json
{
  "job_id": "xxxx",
  "status": "pending"
}
```

---

### Consultar estado del Job

```
GET /api/v1/jobs/{job_id}
```

**Estados posibles:**

* `pending`
* `running`
* `completed`
* `failed`

---

## ✅ Validaciones implementadas

* VLAN ID debe estar entre 1–4094
* VLANs reservadas no permitidas:

  * 1, 1002–1005
* Nombre:

  * Sin espacios
  * Máximo 32 caracteres

---

## ⚙️ Sistema de Jobs (Async)

* Todas las operaciones se ejecutan en segundo plano
* Se retorna un `job_id` inmediatamente
* Permite consultar estado y resultado posteriormente

---

## 🧪 Mock actual

El sistema actualmente simula la ejecución:

```python
return {
    "rc": 0,
    "stdout": f"Mock VLAN {vlan.vlan_id} created on {vlan.device}",
    "stderr": ""
}
```

---

## 📌 Estado actual del proyecto

✔ Endpoint de creación de VLAN
✔ Validaciones de entrada
✔ Sistema de jobs asíncrono
✔ Endpoint de consulta de jobs
✔ Integración mock de ejecución

---

## 🔜 Próximos pasos

* Integración real con Ansible
* Implementación de inventario
* Autenticación y RBAC
* Auditoría de acciones
* Tests automatizados (pytest)

---

## 👥 Uso en equipo

Para replicar el entorno:

1. Clonar repositorio
2. Crear entorno virtual
3. Instalar dependencias
4. Ejecutar `uvicorn`
5. Probar desde Swagger

---

## 🧠 Notas

* No se requiere acceso a dispositivos reales para pruebas
* El sistema está preparado para escalar a ejecución real sin cambios en la API
