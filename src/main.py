import asyncio
import logging

DEFAULT_ROOM = "LOBBY"
MAX_NAME_LENGTH = 16

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class Client:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader: asyncio.StreamReader = reader
        self.writer: asyncio.StreamWriter = writer
        self.name: str | None = None
        self.room_name: str = DEFAULT_ROOM
        self._closing: bool = False

    async def send_message(self, message: str) -> None:
        if self._closing:
            return
        try:
            self.writer.write(f"{message}\r\n".encode("ascii"))
            await self.writer.drain()
        except ConnectionError:
            self._closing = True

    async def disconnect(self) -> None:
        self._closing = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            logger.exception("Error closing client connection")


class ChatServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8888) -> None:
        self.host: str = host
        self.port: int = port
        # State management
        self.clients: set[Client] = set()  # Set of Client objects
        self.rooms: dict[str, set[Client]] = {}  # Dict: room_name -> Set[Client]

    async def start(self) -> None:
        server = await asyncio.start_server(self.handle_client, self.host, self.port)

        addr = server.sockets[0].getsockname()
        logger.info("Serving on %s", addr)

        async with server:
            await server.serve_forever()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = Client(reader, writer)
        addr = writer.get_extra_info("peername")
        logger.info("New connection from %s", addr)

        try:
            # 1. Login Phase
            await self.login_handshake(client)

            # 2. Add to Default Room
            self.clients.add(client)
            await self.join_room(client, DEFAULT_ROOM)

            logger.info("%s logged in and joined %s", client.name, DEFAULT_ROOM)

            # 3. Main Loop
            while True:
                data = await reader.readline()
                if not data:  # EOF (Client disconnected)
                    break

                message = data.decode("ascii").strip()
                if not message:
                    continue

                if message.startswith("/"):
                    await self.handle_command(client, message)
                else:
                    await self.broadcast_chat(client, message)

        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # Expected on disconnect
        except Exception:
            logger.exception("Error handling client %s", addr)
        finally:
            await self.cleanup_client(client)
            logger.info("Connection closed for %s", addr)

    async def login_handshake(self, client: Client) -> None:
        await client.send_message("Welcome! Please enter your display name:")

        while True:
            await client.send_message("Name: ")
            data = await client.reader.readline()
            if not data:
                msg = "Client disconnected during login"
                raise ConnectionError(msg)

            name = data.decode("ascii").strip()

            if not name:
                continue

            if not name.isalnum() or len(name) > MAX_NAME_LENGTH:
                await client.send_message(f"Invalid name. Use alphanumeric characters (max {MAX_NAME_LENGTH}).")
                continue

            if any(c.name.lower() == name.lower() for c in self.clients if c.name):
                await client.send_message(f"The name '{name}' is already taken. Try again.")
                continue

            client.name = name
            await client.send_message(f"Welcome, {client.name}!")
            return

    async def broadcast_chat(self, sender: Client, message: str) -> None:
        room = self.rooms.get(sender.room_name, set())
        formatted_msg = f"{sender.name}: {message}"

        logger.info("Chat: %s", formatted_msg)

        for user in room:
            if user != sender:
                await user.send_message(formatted_msg)

    async def join_room(self, client: Client, room_name: str) -> None:
        room_name = room_name.upper()
        old_room = client.room_name

        if old_room == room_name:
            await client.send_message(f"You are already in room: {room_name}")
            return

        # Remove from old room if exists
        if old_room and old_room in self.rooms:
            self.rooms[old_room].discard(client)
            await self.system_message(old_room, f"{client.name} left the room.")
            # Delete room if empty
            if not self.rooms[old_room]:
                del self.rooms[old_room]
                logger.info("Room %s deleted (empty).", old_room)

        # Add to new room
        client.room_name = room_name
        if room_name not in self.rooms:
            self.rooms[room_name] = set()
            logger.info("Room %s created.", room_name)

        self.rooms[room_name].add(client)
        await client.send_message(f"You joined room: {room_name}")
        await self.system_message(room_name, f"{client.name} joined the room.")

    async def system_message(self, room_name: str, message: str) -> None:
        if room_name in self.rooms:
            for user in self.rooms[room_name]:
                await user.send_message(f"* {message}")

    async def cleanup_client(self, client: Client) -> None:
        if client in self.clients:
            self.clients.remove(client)

        if client.room_name and client.room_name in self.rooms:
            self.rooms[client.room_name].discard(client)
            await self.system_message(client.room_name, f"{client.name} has disconnected.")
            if not self.rooms[client.room_name]:
                del self.rooms[client.room_name]

        await client.disconnect()

    # ==========================================
    # Command Handling Logic
    # ==========================================
    async def handle_command(self, client: Client, command_str: str) -> None:
        parts = command_str[1:].split(" ", 1)
        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Look for a method named cmd_{cmd_name}
        handler = getattr(self, f"cmd_{cmd_name}", None)

        if handler:
            try:
                await handler(client, args)
            except Exception:
                logger.exception("Command error")
                await client.send_message("Error executing command.")
        else:
            await client.send_message(f"Unknown command: {cmd_name.upper()}")

    # --- Commands Definition ---
    # To add a new command, just define async def cmd_name(self, client, args)

    async def cmd_join(self, client: Client, args: str) -> None:
        if not args or not args.strip().isalnum():
            await client.send_message("Usage: /JOIN <room_name>")
            return
        await self.join_room(client, args.strip())

    async def cmd_quit(self, client: Client, args: str) -> None:  # noqa: ARG002, PLR6301
        await client.send_message("Goodbye!")
        await client.disconnect()

    async def cmd_rooms(self, client: Client, args: str) -> None:  # noqa: ARG002
        room_list = ", ".join(f"{name} ({len(users)})" for name, users in self.rooms.items())
        await client.send_message(f"Active Rooms: {room_list}")

    async def cmd_who(self, client: Client, args: str) -> None:  # noqa: ARG002
        if client.room_name in self.rooms:
            users: str = ", ".join(u.name or "(anon)" for u in self.rooms[client.room_name])
            await client.send_message(f"Users in {client.room_name}: {users}")

    async def cmd_help(self, client: Client, args: str) -> None:  # noqa: ARG002
        # Dynamically find commands
        cmds = ["/" + m[4:].upper() for m in dir(self) if m.startswith("cmd_")]
        await client.send_message(f"Available commands: {', '.join(cmds)}")


async def main() -> None:
    chat_server = ChatServer("0.0.0.0", 8888)  # noqa: S104
    await chat_server.start()


if __name__ == "__main__":
    asyncio.run(main())
