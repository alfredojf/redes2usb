import sys
import argparse
import servidorbase
import threading
import socket


class ServidorCentral(servidorbase.ServidorBase):
    "Clase para el servidor central del proyecto"

    DEFAULT_DATA = {'clientes': {}, 'videos': {}}  # datos que se cargan cuando no hay archivo

    def __init__(self, *args):
        "Inicializacion del servidor central"
        super().__init__(*args)
        self.secundarios = {}           # Datos no persistentes (informacion de los secundarios)
        self.lock = threading.Lock()    # mutex para proteger atributos
        self.sinc = False               # marca si ya se hizo la sincronizacion o no
        self.videos = set()             # Conjunto de videos disponibles
        self.MENSAJES = {  # mensajes y sus respectivos manejadores
            'inscripcion': self.inscripcion,
            'sincronizacion': self.sincronizacion,
            'listado': self.listado,
            'descarga': self.descarga,
            'completado': self.completado,
            'caido': self.caido
        }

    def setup(self):
        pass

    def msg_handler(self, msg, handler):
        "Manejador general, que escoge el especifico dependiendo del tipo de mensaje"
        try:
            operacion = self.MENSAJES[msg['accion']]
        except:
            print('Se recibio mensaje mal formado desde ' + str(handler.client_address))
        else:
            operacion(msg, handler)

    def inscripcion(self, msg, handler):
        "Manejador de las inscripciones. Guarda los nuevos datos y envia mensajes de sincronizacion"
        handler.request.close()
        with self.lock:
            if self.sinc:  # esto ocurre si un servidor caido se esta levantando
                self.secundarios[(handler.client_address[0], msg['puerto'])] = msg['videos']
                print(
                    "Se recupero conexion con el servidor secundario %s:%d" % (
                        handler.client_address[0], msg['puerto'])
                )
                return
            nuevo_videos = msg["videos"]
            nuevo_serv = (handler.client_address[0], msg["puerto"])
            resp = {nuevo_serv: []}
            # Se arma el contenido de los mensajes de sincronizacion
            for server, videos in self.secundarios.items():
                resp[nuevo_serv].append({
                    "ip": server[0], "puerto": server[1], "videos": videos
                })
                resp[server] = [{
                    "ip": nuevo_serv[0], "puerto": nuevo_serv[1], "videos": nuevo_videos
                }]
            # Se registran los nuevos videos en el servidor central
            self.secundarios[nuevo_serv] = nuevo_videos
            for v in nuevo_videos:
                self.videos.add(v)
                if v not in self.data['videos']:
                    self.data['videos'][v] = 0
        # envio de los mensajes de sincronizacion
        for destino, contenido in resp.items():
            msg = {"accion": "sincronizacion", "servidores": contenido}
            socket_dest = socket.socket()
            socket_dest.connect(destino)
            self.msg_send(msg, socket_dest)
            socket_dest.close()

    def sincronizacion(self, msg, handler):
        """ Manejador de notificaciones de sincronizacion.
            Actualiza los datos sobre los servidores secundarios e identifica si la sincronizacion
            ya termino
        """
        with self.lock:
            if self.sinc:
                return
            self.secundarios[(handler.client_address[0], msg["puerto"])].extend(msg['videos'])
            print('Servidor %s sincronizo videos "%s"' % (
                str((handler.client_address[0], msg["puerto"])), msg['videos']))
            if all(len(v) == len(self.videos) for _, v in self.secundarios.items()) and \
               len(self.secundarios) == 3:
                self.sinc = True
                print("Sincronizacion completa")

    def listado(self, msg, handler):
        """ Manejador de peticion de lista de videos.
            Responde con la lista vacia si aun no ha terminado la sincronizacion
        """
        with self.lock:
            sinc = self.sinc
        if not sinc:
            self.msg_send([], handler.request)
        else:
            # Set is not JSON serializable, it must be converted to list.
            self.msg_send(list(self.videos), handler.request)

    def descarga(self, msg, handler):
        """ Manejador de peticion de descarga.
            Responde si el video existe, no existe o aun no ha culminado la sincronizacion.
            Si existe, se envia la lista de servidores secundarios activos.
        """
        with self.lock:
            sinc = self.sinc
            encontrado = msg['video'] in self.videos
            servidores = []
            if sinc and encontrado:
                for servidor, _ in self.secundarios.items():
                    servidores.append({'ip': servidor[0], 'puerto': servidor[1]})
            resp = {'servidores': servidores}
        if not sinc:
            resp['resultado'] = 'espera'
        elif encontrado:
            resp['resultado'] = 'hallado'
        else:
            resp['resultado'] = 'no hallado'
        self.msg_send(resp, handler.request)

    def completado(self, msg, handler):
        "Manejador de notificacion de descarga completada. Actualiza las estadisticas"
        with self.lock:
            self.data['videos'][msg['video']] += 1
            if msg['nombre'] in self.data['clientes']:
                self.data['clientes'][msg['nombre']] += 1
            else:
                self.data['clientes'][msg['nombre']] = 1
        print("El cliente %s descargo el video %s" % (msg['nombre'], msg['video']))

    def caido(self, msg, handler):
        "Manejador de notificacion de servidor caido. Actualiza lista de servidores secundarios"
        with self.lock:
            del self.secundarios[(msg["ip"], msg['puerto'])]
        print("Se perdio conexion con el servidor secundario %s:%d" % (msg["ip"], msg["puerto"]))

    def command_handler(self, command, arg):
        "Ejecucion de comandos"
        if command.upper() == "NUMERO_DESCARGAS_VIDEO":
            with self.lock:
                print('video|descargas')
                for nombre, veces in self.data['videos'].items():
                    print("%s|%d" % (nombre, veces))
        elif command.upper() == "VIDEOS_CLIENTE":
            with self.lock:
                print('cliente|descargas')
                for nombre, veces in self.data['clientes'].items():
                    print("%s|%d" % (nombre, veces))
        elif command.upper() in ['H', 'HELP']:
            print('NUMERO_DESCARGAS_VIDEO')
            print('VIDEOS_CLIENTE')
        else:
            print("Comando no reconocido")


def main(args):
    parser = argparse.ArgumentParser(
        prog=args[0],
        description=("Servidor central para descarga de videos por trozos." +
                     "Proyecto de Redes II, Septiembre-Diciembre 2017, USB.")
    )
    parser.add_argument(
        "--ip", "-i", action="store", help="Dirección IP donde se escuchará", default="0.0.0.0",
        type=servidorbase.direccion_ip
    )
    parser.add_argument(
        "--puerto", "-p", action="store", help="Puerto donde se escuchará", default=50000,
        type=int
    )
    pargs = parser.parse_args(args=args[1:])

    servidor = ServidorCentral(pargs.ip, pargs.puerto, 'central.json')
    servidor.run("Servidor escuchando en %s:%d" % (pargs.ip, pargs.puerto))


if __name__ == '__main__':
    main(sys.argv)
