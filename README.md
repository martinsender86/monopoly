# MonoPoly — v1.5

Unir, separar y editar archivos de sonido de rodaje (WAV / BWF). Windows y Mac. Antes se llamaba WavPoly.

## Qué hace

Reemplazo moderno del viejo **Wave Agent**:

1. **Unir** varios archivos **mono** en un solo archivo **poly** (multicanal). Los agrupa por timecode, por nombre (mismo prefijo con distinto sufijo de pista) y/o por duración.
2. **Separar** un archivo poly en sus monos.
3. **Editar la metadata** de rodaje: escena, toma, tape, proyecto, notas, nombres de pista, frame rate y TC de inicio (el TC, detrás de un candado). Siempre se escribe en una **copia nueva**.

Sirve, por ejemplo, para preparar el sonido de un grabador para Avid, Premiere o Resolve, recuperar polys cuando el grabador entregó monos sueltos, o completar la metadata de grabadores que no la escriben (como los Zoom).

## La regla principal: el audio no se toca

- Las muestras se copian **byte a byte**: no cambian el sample rate, los bits, la duración ni el timecode. Si unís y después separás, obtenés archivos idénticos a los originales.
- Para unir, sample rate, bits, formato, duración y timecode tienen que coincidir exacto. Nunca rellena, recorta ni convierte.
- Todos los chunks de metadata (bext, iXML, LIST, cue…) se copian tal cual. Solo cambia lo que describe los canales o lo que el usuario edita.
- Un RF64/BW64 sigue siendo RF64; un RIFF pasa a RF64 solo si supera 4 GB.
- Los cálculos de tiempo usan fracciones exactas, sin redondeos.

## Uso

- **Windows:** doble clic en `MonoPoly.exe` (en Releases). Si aparece "Windows protegió su PC": Más información > Ejecutar de todas formas.
- **Mac:** compilá la app con `Mac/COMPILAR_MAC.command` (necesita Python 3 de python.org). La primera vez, clic derecho > Abrir. Una app compilada en Apple Silicon no corre en Intel, y al revés.
- Para compilar en Windows: `Windows/COMPILAR_WINDOWS.bat`.

Manual completo en `Windows/LEEME.txt` (es el mismo en Mac).

## Contenido

| Archivo | Para qué |
|---|---|
| `wavpoly.py` | Motor: lectura/escritura de WAV, unir, separar, metadata (también usable por línea de comandos) |
| `wavpoly_gui.py` | Interfaz gráfica |
| `COMPILAR_*` | Arman la app (`.exe` / `.app`) |
| `LEEME.txt` | Manual |

El código es el mismo en `Windows/` y `Mac/`; cada carpeta tiene su script de compilación.

## Descargas

`MonoPoly.exe` (Windows) en **Releases**.
