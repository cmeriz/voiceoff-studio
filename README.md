# VoiceOff Studio

Aplicación web que quita la voz de una canción MP3 o WAV y genera su versión karaoke. La separación se hace con IA ([Demucs v4](https://github.com/facebookresearch/demucs)) y se ejecuta por completo en tu equipo.

## Puesta en marcha

Solo necesitas Docker con el motor en marcha (en Windows, abre Docker Desktop).

```sh
docker compose up -d --build
```

Abre <http://127.0.0.1:8000>. La primera construcción tarda varios minutos porque descarga PyTorch y el modelo; la imagen ocupa unos 1,6 GB.

```sh
docker compose logs -f app   # ver los registros
docker compose down          # detener (las pistas se conservan)
docker compose down -v       # detener y borrar también las pistas
```

## Cómo funciona

1. Subes uno o varios archivos. El backend los valida con `ffprobe` según su contenido, no según su extensión.
2. Un único worker procesa las canciones por orden, cada una en un subproceso de Demucs que libera su memoria al terminar.
3. Cuando termina, el interruptor **Voz** alterna entre el karaoke y el original en la misma posición, y puedes descargar el resultado en el formato de entrada.
4. El control **Tono** sube o baja el karaoke hasta 6 semitonos sin cambiar su velocidad. La primera vez, el servidor genera esa versión con FFmpeg y Rubber Band (unos 6 s por minuto de audio) mientras sigues escuchando el tono anterior; después queda guardada. Con un tono distinto del original, el interruptor **Voz** se desactiva.
5. Las pistas se borran automáticamente a las 24 horas, con todas sus versiones de tono. También puedes borrarlas antes con la **X**, que pide confirmación porque el borrado es definitivo. En la cabecera de la lista, **Borrar cambios de tono** elimina solo las versiones de tono y conserva las canciones, y **Eliminar todo** borra todas las pistas, incluidas las que estén en proceso o en cola.

Si el contenedor se reinicia, las pistas terminadas se conservan. Las que estaban en cola o en proceso se marcan como fallidas y hay que volver a subirlas.

## Configuración

Variables de entorno, que puedes definir en un archivo `.env`:

| Variable | Valor por defecto | Descripción |
|---|---|---|
| `PORT` | `8000` | Puerto local |
| `MAX_UPLOAD_MB` | `100` | Tamaño máximo por archivo |
| `MAX_DURATION_SECONDS` | `900` | Duración máxima por canción |
| `RETENTION_HOURS` | `24` | Tiempo que se conservan las pistas |

La aplicación solo escucha en `127.0.0.1` porque no tiene autenticación. Añade autenticación y límites de uso antes de exponerla a otra red.

## Limitaciones

- El resultado es la pista instrumental: no incluye letra sincronizada.
- Pueden quedar restos de voz o reverberación, y los coros pueden desaparecer o quedar a medias.
- La imagen usa PyTorch para CPU, así que el procesamiento es lento.

## API

| Método y ruta | Descripción |
|---|---|
| `POST /api/jobs?filename=cancion.mp3` | Crea un trabajo; el cuerpo es el archivo en binario |
| `GET /api/jobs` | Lista los trabajos |
| `GET /api/jobs/{id}` | Consulta el estado y el progreso |
| `GET /api/jobs/{id}/original` | Devuelve el audio original |
| `GET /api/jobs/{id}/karaoke` | Devuelve el resultado; admite `?semitones=2` para un tono ya generado y `?download=true` para descargarlo |
| `POST /api/jobs/{id}/pitch?semitones=2` | Pide un tono de −6 a +6; responde 202 mientras se genera y 200 si ya está listo |
| `DELETE /api/jobs/{id}` | Cancela o elimina un trabajo |
| `DELETE /api/jobs` | Cancela y elimina todos los trabajos |
| `DELETE /api/pitch-versions` | Elimina todas las versiones de tono y conserva las canciones |
| `GET /api/config` | Devuelve los límites configurados |
| `GET /api/health` | Comprueba el estado del servicio |

Ejemplo:

```sh
curl --data-binary @cancion.mp3 "http://127.0.0.1:8000/api/jobs?filename=cancion.mp3"
```

## Desarrollo

```
app/            Backend FastAPI (API, cola de trabajos, Demucs, FFmpeg)
app/static/     Interfaz en HTML, CSS y JavaScript, sin paso de build
app/static/brand/   Logo en SVG (versión oscura y clara, icono y favicon)
tests/          Pruebas; usan un sustituto de Demucs, así que tardan segundos
```

Ejecutar las pruebas:

```sh
docker compose run --rm --build test
```

Las dependencias directas están en `requirements.in`, y `requirements.txt` es el resultado compilado para Linux. Para regenerarlo:

```sh
docker run --rm -v "${PWD}:/work" -w /work python:3.12-slim-bookworm sh -c "pip install uv==0.5.11 && uv pip compile requirements.in -o requirements.txt --python-version 3.12 --python-platform linux --index-strategy unsafe-best-match --emit-index-url --no-header"
```

## Licencias

La fuente Inter Tight se distribuye bajo la licencia SIL OFL (`app/static/fonts/LICENSE-inter-tight.txt`).

El cambio de tono usa Rubber Band a través del paquete FFmpeg de Debian. Rubber Band tiene licencia GPL, lo que no afecta al uso personal pero debe revisarse antes de distribuir o vender la aplicación.
