# TransmiEye 2.0

Sistema de detección en tiempo real de personas que entran a las estaciones de TransMilenio por las **puertas laterales** (desde el carril exclusivo), con OpenCV y YOLO. Incluye monitoreo web de varias cámaras, alertas al instante, evidencia anonimizada con cadena de custodia, revisión por operadores, historial, reportes PDF, usuarios con roles y purga automática de imágenes.

## 1. Instalación

Requisitos: Python 3.10 a 3.12. Una GPU NVIDIA es opcional, pero multiplica la velocidad.

**Windows:** doble clic en `iniciar_windows.bat`. La primera vez crea un entorno virtual e instala todo (unos 2 GB por PyTorch). Las siguientes veces solo arranca.

**Linux / macOS:** `./iniciar_linux.sh`

**Manual:**

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python run.py                     # http://localhost:8000
python run.py --red               # para entrar desde otros equipos de la red
```

**Con GPU NVIDIA:** instala primero PyTorch con CUDA desde https://pytorch.org/get-started/locally/ (por ejemplo `pip install torch --index-url https://download.pytorch.org/whl/cu124`) y después `pip install -r requirements.txt`. En Configuración → Sistema verás el nombre de la GPU.

Si PyTorch o Ultralytics no se pueden instalar, el sistema sigue funcionando con detección por movimiento (MOG2). Es menos precisa y no reconoce buses.

## 2. Primeros pasos

1. Abre http://localhost:8000 e ingresa con **admin / admin123**. Cambia la contraseña en *Configuración → Mi cuenta*.
2. En **Cámaras → Agregar cámara**, elige el origen del video:
   - **Cámara USB:** el número 0 es la primera webcam conectada.
   - **Cámara IP:** su URL RTSP o HTTP. Ejemplos:
     - Hikvision: `rtsp://usuario:clave@192.168.1.64:554/Streaming/Channels/101`
     - Dahua: `rtsp://usuario:clave@IP:554/cam/realmonitor?channel=1&subtype=0`
     - Celular con la app *IP Webcam* (Android): `http://IP-del-celular:8080/video`. Es una forma barata de tener una cámara de prueba.
   - **Archivo de video:** súbelo desde el formulario o cópialo a la carpeta `videos/`. Se reproduce en bucle a velocidad real, como una cámara en vivo.
3. Usa **Probar conexión** y luego guarda. La cámara empieza a procesarse de inmediato.
4. Abre **Zonas y puertas** y marca con clics sobre la imagen:
   - **Carril (fuera):** la vía frente a las puertas, hasta el borde de la puerta.
   - **Plataforma (dentro):** el piso de la estación justo detrás de las puertas.
   - **Bahía del bus (opcional):** donde queda detenido el bus. Si no la dibujas, se usa el carril.
   - **Línea de puerta (recomendada):** dos clics sobre el umbral de cada puerta. La flecha se orienta sola hacia la plataforma: nace en el lado rojo y apunta al verde, y ese es el sentido que cuenta como **ingreso**. El sentido contrario es **salida**. Si queda al revés, usa *Invertir sentido*.

   Deja una franja pequeña sin zona sobre el umbral de las puertas y pon ahí la línea. Lo más preciso es usar zonas y líneas juntas.

Sobre el video, cada puerta aparece con su línea amarilla y una flecha roja y verde que indica el sentido de ingreso. Cuando alguien entra sin bus, la línea destella en rojo y la persona queda marcada como `EVASION`. En la esquina superior se ven los contadores de la cámara: ingresos irregulares, salidas y cruces con bus.

### Dónde ubicar la cámara

- **Desde dentro de la estación** (como tu foto marcada «Dentro/Fuera»): la cámara en alto, mirando hacia las puertas. El carril queda al fondo y la plataforma adelante.
- **Desde fuera** (como la foto de Héroes, desde el andén o un puente): el carril queda adelante y la plataforma detrás de las puertas.

En ambos casos lo que importa es que se vean **los pies** de las personas a los dos lados de la puerta. El sistema decide el lado según dónde están los pies, no la cabeza. Mientras más alta y perpendicular a las puertas esté la cámara, menos oclusiones habrá.

## 3. Cómo detecta

Cada cámara corre en su propio hilo:

```
captura (USB/RTSP/archivo, con reconexión automática)
  → YOLO detecta personas y buses
  → seguimiento: cada persona conserva un ID entre cuadros
  → reglas sobre el punto de los pies de cada persona
  → video anotado, evento en la base de datos, alerta por WebSocket
```

Un cruce se detecta de dos formas complementarias y se cuenta **una sola vez por persona**:

- **Por zonas:** la persona se confirma en el rojo y después en el verde.
- **Por línea de puerta:** los pies quedan de un lado de la línea y luego del otro, a una distancia mínima relativa a la altura de la persona. Así se detecta a quien cruza rápido y aparece un solo cuadro del lado del carril (pasa a 4–8 FPS en CPU), sin contar a quien está parado sobre el umbral.

| Evento | Regla | Nivel |
|---|---|---|
| **Ingreso por puerta lateral** | Los pies pasan de *carril* a *plataforma*, o cruzan la línea en el sentido de la flecha, sin bus en la bahía ni recién salido | Crítica |
| Persona en el carril | Más de N segundos en el carril sin bus | Media |
| Salida por puerta lateral | Pasa de *plataforma* a *carril*, o cruza la línea contra la flecha, sin bus. Cada persona cuenta: no hay pausa entre salidas | Media |
| Aglomeración en puertas | N o más personas en la plataforma durante 5 s | Baja |

Para no confundir pasajeros con evasores, el sistema aplica tres protecciones:

- Si hay un bus detenido, los cruces se consideran abordaje normal.
- Esa protección dura unos segundos más después de que el bus sale (*gracia*).
- Una persona que estuvo en el carril mientras había bus (es decir, que se bajó) no genera evasión.

Un lado solo se confirma tras varios cuadros seguidos, así que el ruido de un solo cuadro no cuenta. Si alguien sale al carril, no se genera además la alerta de «persona en el carril».

**Personas tapadas por el marco de la puerta:** al cruzar, el marco suele tapar a la persona un instante y el tracker le asigna otro número. Sin corrección, ese cruce se perdería. El sistema une la pista nueva con la perdida (*empalme*) solo si se cumplen todas estas condiciones:

- las dos están junto a la misma puerta y en lados opuestos;
- pasaron menos de 0,8 s;
- la distancia es compatible con caminar;
- la altura y la forma de la caja son parecidas;
- hay un único candidato claro. En una multitud ambigua no se adivina.

Estos eventos salen marcados como *ID recuperado tras oclusión* y con 15 % menos de confianza, para que el operador los revise con el clip.

Estos criterios se calibraron mirando empalmes reales. Una primera versión más permisiva (1,5 s, velocidad de trote) llegó a unir personas distintas, e incluso un muñeco gigante detectado como persona. Con los criterios actuales, en un video de multitud el empalme actúa unas 8 veces por minuto y la mayoría corresponde a la misma persona. Se puede desactivar con 0 en *Recuperar personas tapadas por la puerta*.

**Por qué no se usa la clase «tren» de YOLO:** al probar con las fotos de las estaciones, YOLO detectó correctamente el bus rojo como `bus`, pero clasificó la estructura de la estación como `train` sobre el 75 % de la imagen. Por eso solo cuentan `bus` y `truck` como vehículo. Es configurable.

**Seguimiento:** en CPU la inferencia suele correr a 4–15 FPS. A esos FPS, ByteTrack dejaba sin pista a cerca de la mitad de las personas, porque necesita dos cuadros seguidos parecidos para confirmar una pista nueva. Por eso el sistema usa por defecto un tracker propio:

- asigna un ID en el mismo cuadro;
- predice la posición con la velocidad;
- asocia por solapamiento o por la distancia entre los pies relativa a la altura de la persona.

ByteTrack sigue disponible en Configuración para equipos con GPU.

**Evidencia:** cada evento guarda una imagen y un clip de unos 6 s (4 s antes y 2 s después). Los rostros se pixelan antes de guardar (Ley 1581 de 2012): siempre se cubre la cabeza de cada persona detectada, más un detector de rostros adicional. Se registra el hash SHA-256 de la imagen. El ZIP de evidencia incluye la cadena de custodia y verifica que la imagen no haya sido alterada.

## 4. Ajuste fino

Todo se ajusta en *Configuración → Detección* y se aplica en caliente.

| Si pasa esto | Prueba |
|---|---|
| No detecta personas lejanas | Subir *Tamaño de inferencia* a 960 o 1280, bajar *Confianza para personas* a 0.25, o usar `yolov8s.pt` |
| Va lento (FPS bajos) | Bajar el tamaño de inferencia a 640 o el ancho de procesamiento a 800, o usar GPU |
| Alerta cuando la gente se baja del bus | Subir *Segundos de gracia* o dibujar la *Bahía del bus* |
| Demasiadas alertas de «persona en el carril» | Subir *Segundos en el carril* o la *Pausa entre alertas* |
| Eventos sin alerta | Bajar *Confianza mínima para alertar* |
| No cuenta a quien cruza muy rápido | Dibujar una línea de puerta; bajar *Distancia mínima a la línea* a 0.1 |
| Cuenta a gente parada en la puerta | Subir *Distancia mínima a la línea* a 0.25 o *Cuadros para confirmar un lado* a 3 |
| Une personas distintas en multitudes | Bajar *Recuperar personas tapadas por la puerta* a 0.5 o a 0 |

La **precisión real** se mide sola: cada vez que un operador confirma o rechaza un evento, el panel calcula *confirmados / revisados*. Esto sirve para el HU20 y el criterio de aceptación de precisión ≥ 85 %.

**Mejora recomendada:** reentrenar YOLO con video propio de TransMilenio. Etiqueta unas 300–500 imágenes de las puertas con personas y buses, entrena con `yolo train model=yolov8n.pt data=tm.yaml epochs=80` y copia el `best.pt` resultante a `modelos/`. Aparecerá en la lista de modelos.

## 5. Estructura

```
backend/
  run.py                  arranque del servidor
  app/main.py             API REST, WebSocket, streaming MJPEG
  app/db.py               modelo relacional (estación, cámara, tipo, evento, alerta, operador, reporte, logs)
  app/auth.py             bcrypt + JWT, roles admin/supervisor/visor
  app/events.py           guardado de evidencia, hash y alertas en tiempo real
  app/reports.py          reportes PDF
  app/maintenance.py      purga automática por retención
  app/vision/source.py    captura con reconexión
  app/vision/detectors.py YOLO, trackers, detector de movimiento
  app/vision/analyzer.py  zonas y reglas de puertas laterales
  app/vision/render.py    anonimización y video anotado
  app/vision/worker.py    hilo por cámara
  tests/                  pruebas del motor de reglas
frontend/                 interfaz web (HTML, CSS y JS sin dependencias)
modelos/                  pesos YOLO (.pt)
videos/                   videos de prueba
data/                     base de datos, evidencias y reportes (se crea solo)
```

- **Documentación interactiva de la API:** http://localhost:8000/docs
- **Pruebas:** `cd backend && python tests/test_analizador.py`. Son 21 casos: ingreso sin bus, cruce rápido de un solo cuadro, cambio de ID en la puerta, multitud ambigua, diagonal, dos personas juntas, bus en la puerta y gracia, ruido, persona parada sobre el umbral, salidas múltiples, permanencia y aglomeración.
- **PostgreSQL** en lugar de SQLite: `set TRANSMIEYE_DB=postgresql+psycopg2://usuario:clave@localhost:5432/transmieye` e instala `psycopg2-binary`.

## 6. Trazabilidad con el documento de la práctica

| Requisito | Dónde está |
|---|---|
| RF01 Captura de video (RTSP), RF09 Gestión de cámaras | Página Cámaras; `source.py` |
| RF02 Detección de personas YOLO, RF03 ROI | `detectors.py`; editor de zonas |
| RF04 Detección de evasión | `analyzer.py`, adaptado a puertas laterales |
| RF05 Registro de eventos, RF06 Alertas | `events.py`; WebSocket; página Alertas |
| RF07 / RNF03 Anonimización | `render.py`; toda la evidencia se pixela antes de guardar |
| RF08 Dashboard, RF16 Estadísticas | Panel |
| RF10 Historial con filtros, RF11 Confirmación manual | Historial y detalle del evento |
| RF12 Reportes PDF | Reportes |
| RF13 Usuarios y roles, RF14 Autenticación | Configuración → Usuarios; bcrypt + JWT |
| RF15 / RNF15 Logs técnicos y de acceso | Configuración → Registros |
| RF17 Umbrales configurables | Configuración → Detección |
| RF18 Exportación de evidencia | Botón *Exportar evidencia* (ZIP con SHA-256) |
| RNF07 Concurrencia de cámaras | Un hilo por cámara |
| RNF11 Reconexión ≤ 10 s | Reintentos 1, 2, 4, 8 y 10 s |
| RNF13 / HU19 Purga a 30 días | `maintenance.py`, diaria y también manual |
| HU20 Métricas | Precisión por revisión de operadores; FPS y latencia por cámara |

**Cambios respecto al diseño original:** los tres tipos de evasión del documento (salto, contraflujo y paso simultáneo) corresponden a torniquetes. Este sistema se enfoca en las puertas laterales, con los cuatro eventos de la sección 3. La tabla `CAMARA` usa el campo `fuente` (URL, índice o archivo) en lugar de `ip_address`, para admitir cualquier origen de video.
