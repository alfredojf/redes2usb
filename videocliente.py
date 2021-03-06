import sys
import argparse
import signal
import socket
import json
import threading
from servidorbase import ServidorBase
import queue
import shutil
import struct
import tempfile


class Cliente():
    "Clase para el cliente"

    def __init__(self, ip_central, puerto_central):
        "Constructor de la clase"
        self.ip_central = ip_central
        self.puerto_central = puerto_central
        self._nombre = None
        self.nombre_lock = threading.Lock()

    @property
    def nombre(self):
        "Propiedad nombre, protegida por un lock"
        with self.nombre_lock:
            return self._nombre

    @nombre.setter
    def nombre(self, val):
        with self.nombre_lock:
            self._nombre = val

    def run(self):
        "Lector de comandos"
        def signal_handler(signal, frame):
            sys.exit(0)
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        while True:
            try:
                text = input()
            except EOFError:
                return
            splitted = text.split(' ')
            if len(splitted) == 1:
                self.command_handler(splitted[0], None)
            elif len(splitted) > 1:
                self.command_handler(splitted[0], splitted[1])

    def caido(self, servidor):
        "Metodo que notifica al servidor central de un servidor secundario caido"
        try:
            central = socket.socket()
            central.connect((self.ip_central, self.puerto_central))
            ServidorBase.msg_send(
                {"accion": "caido", 'ip': servidor['ip'], 'puerto': servidor['puerto']},
                central
            )
            central.close()
        except:
            return "No se logro notificar al servidor central"
        else:
            return "Se le notifico al servidor central"

    def command_handler(self, command, arg):
        "Ejecucion de comandos"
        if command.upper() == "INSCRIBIR":
            if arg is None:
                print("Falta parametro: INSCRIBIR <nombre>")
            else:
                self.nombre = arg
        elif command.upper() == "LISTA_VIDEOS":
            self._lista_videos()
        elif command.upper() == "VIDEO":
            if arg is None:
                print("Falta parametro: VIDEO <nombre>")
            else:
                self._video(arg)
        elif command.upper() in ['H', 'HELP']:
            print('INSCRIBIR <nombre>')
            print('LISTA_VIDEOS')
            print('VIDEO <nombre>')
        else:
            print("Comando no reconocido: {}".format(command))

    def msg_read(self, socket):
        "Metodo para leer un mensaje JSON de un socket"
        buffer = bytearray()
        data = socket.recv(1024)
        while len(data) > 0:
            buffer.extend(data)
            data = socket.recv(1024)
        return json.loads(buffer.decode('utf-8'))

    def _lista_videos(self):
        """ Pregunta al servidor central cuál es la lista de videos para descargar
        """
        try:
            socket_central = socket.socket()
            socket_central.connect((self.ip_central, self.puerto_central))
            ServidorBase.msg_send({"accion": "listado"}, socket_central)
            lista_videos = self.msg_read(socket_central)
            socket_central.close()
            print("Videos disponibles:")
            for v in lista_videos:
                print(v)
        except:
            print("No se pudo establecer conexión con el servidor central")

    def _video(self, nombre):
        """ Descarga de video. Si el video existe, se descarga en otro hilo.
            nombre: nombre del video
        """
        if self.nombre is None:
            print("Debes inscribir un nombre primero")
            return
        try:
            socket_central = socket.socket()
            socket_central.connect((self.ip_central, self.puerto_central))
            ServidorBase.msg_send({"accion": "descarga", "video": nombre}, socket_central)
            respuesta = self.msg_read(socket_central)
            socket_central.close()
        except:
            print("No se pudo establecer conexión con el servidor central")
            return

        if respuesta['resultado'] == "espera":
            print("Los servidores no se ha sincronizado. Intente más tarde")
        elif respuesta['resultado'] == "no hallado":
            print("El video no existe.")
        elif respuesta['resultado'] == "hallado":
            print("Video hallado. En breve empezará la descarga")
            descarga = threading.Thread(
                target=self._descarga, args=(nombre, respuesta['servidores']), daemon=True
            )
            descarga.start()

    def _descarga(self, video, servidores):
        """ Descarga desde secundarios.
            Se lanza un hilo encargado de cada trozo, luego se recolectan los resultados.
            Si la descarga de los trozos fue exitosa, se crea el archivo final y se notifica.
            video: nombre del video
            servidores: servidores secundarios que se recibieron del principal
        """
        q = queue.Queue()
        threads = []
        for parte in [0, 1, 2]:  # se lanzan los hilos para los trozos
            t = threading.Thread(
                target=self._descarga_parte, args=(q, parte, video, servidores)
            )
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        resultado = []
        for i in range(3):  # se recolectan los resultados
            resultado.append(q.get())
            if not resultado[-1][0]:
                print("Fallo la descarga del video %s" % video)
                return
        resultado.sort(key=lambda x: x[1])
        with open(video, mode='wb') as f:  # creacion del archivo final
            for a, b, temp in resultado:
                shutil.copyfileobj(temp, f)
                temp.close()
        print("Se completo la descarga del video %s" % video)
        try:   # Notificacion para el servidor central
            central = socket.socket()
            central.connect((self.ip_central, self.puerto_central))
            ServidorBase.msg_send(
                {"accion": "completado", "nombre": self.nombre, "video": video}, central
            )
            central.close()
        except:
            print("No pudo notificarse al servidor central")

    def _descarga_parte(self, salida, parte, video, servidores):
        "Descarga de una parte. Se intentan todos los servidores hasta que uno funciona, o se falla"
        for i in range(len(servidores)):
            # Primero se establece la conexion
            servidor = servidores[(parte + i) % len(servidores)]
            print("Descargando video %s, trozo %d, desde servidor %s" % (
                    video, parte, servidor["ip"] + str(servidor["puerto"])))
            mensaje_fallo = "Fallo descarga de video %s, trozo %d, desde servidor %s:%d" % (
                video, parte, servidor['ip'], servidor['puerto']
            )
            try:

                conexion = socket.socket()
                conexion.connect((servidor['ip'], servidor['puerto']))
                ServidorBase.msg_send(
                    {"accion": "descarga", "nombre": self.nombre, "parte": parte, "video": video},
                    conexion
                )
            except Exception as e:
                print(mensaje_fallo + "\n" + self.caido(servidor))
                continue

            # Luego se descarga el tamaño. (Esto esta basado en los docs de python)
            try:
                recibido = 0
                trozos = []
                while recibido < 4:
                    trozo = conexion.recv(4 - recibido)
                    if trozo == b'':
                        raise RuntimeError("Socket 1")
                    trozos.append(trozo)
                    recibido += len(trozo)
                tam = struct.unpack("!i", b''.join(trozos))[0]
            except Exception as e:
                print(mensaje_fallo + "\n" + self.caido(servidor))
                continue

            # Finalmente se descarga el video y se guarda en un archivo temporal
            temp = tempfile.TemporaryFile()
            try:
                recibido = 0
                while recibido < tam:
                    trozo = conexion.recv(min(tam - recibido, 2048))
                    if trozo == b'':
                        raise RuntimeError("Socket 2")
                    recibido += len(trozo)
                    temp.write(trozo)
            except Exception as e:
                temp.close()
                print(mensaje_fallo + "\n" + self.caido(servidor))
                continue
            else:
                temp.seek(0, 0)
                salida.put((True, parte, temp))
                print("Descarga exitosa de video %s, trozo %d" % (video, parte))
                return
        salida.put((False, parte, None))


def main(args):
    parser = argparse.ArgumentParser(
        prog=args[0],
        description=("Cliente para descarga de videos por trozos." +
                     "Proyecto de Redes II, Septiembre-Diciembre 2017, USB.")
    )
    parser.add_argument(
        "--ip-central", action='store', help='Dirección IP del servidor central',
        default="localhost"
    )
    parser.add_argument(
        "--puerto-central", action='store', help='Puerto donde escucha el servidor central',
        default=50000, type=int
    )
    pargs = parser.parse_args(args=args[1:])
    cliente = Cliente(pargs.ip_central, pargs.puerto_central)
    cliente.run()


if __name__ == '__main__':
    main(sys.argv)
