import asyncio
from mcp.server.fastmcp import FastMCP


def create_server(environment, port=23457):
    server = FastMCP('StudioMCP', host='127.0.0.1', port=port)
    from .tools import register_mcp
    register_mcp(server, environment)

    @server.resource('studiomcp://skill')
    def skill_resource() -> str:
        from pathlib import Path
        source = Path(__file__).resolve().parents[1] / 'SKILL.md'
        return source.read_text(encoding='utf-8') if source.is_file() else ''

    @server.tool()
    def skill() -> str:
        """Read StudioMCP usage instructions and command contracts."""
        return skill_resource()

    @server.tool()
    async def listStudios() -> list[dict]:
        """List connected Studios (one entry each, with Edit / Play/server / Play/client contexts) and their numbers."""
        return environment.studio_targets()

    @server.tool()
    async def selectStudio(studio: int) -> dict:
        """Persist the active Studio receiver for subsequent calls without a studio argument."""
        return await asyncio.to_thread(environment.select_studio, environment.studio_at(studio))

    @server.tool()
    async def camera(action: str = 'status', studio: int | None = None) -> dict:
        """Query native camera state or reset an Edit camera to Custom without moving it."""
        return await asyncio.to_thread(environment.camera, action, environment.studio_at(studio))

    @server.tool()
    async def savePlugin(directory: str | None = None) -> dict:
        """Build and install the Studio receiver plugin into Roblox Studio."""
        return await asyncio.to_thread(environment.save_plugin, directory)

    @server.tool()
    async def output(studio: int | None = None) -> dict:
        """Read Roblox Output console messages, condensing consecutive identical messages with counts."""
        return await asyncio.to_thread(environment.output, environment.studio_at(studio))

    @server.tool()
    async def view(path: str = 'game', studio: int | None = None, rig: str | None = None, angle: str | None = None, samples: int = 9, appearance: str = 'avatar', rigType: str | None = None, cameraCframe: list[float] | None = None, contactSheet: bool = False):
        """View game from its camera; stored or Workspace models use isolated multi-angle captures. contactSheet captures child Models/BaseParts."""
        import base64
        from mcp.server.fastmcp.utilities.types import Image
        result = await asyncio.to_thread(environment.view, environment.studio_at(studio), path, rig=rig, angle=angle, samples=samples, appearance=appearance, rig_type=rigType, camera_cframe=cameraCframe, contact_sheet=contactSheet)
        return Image(data=base64.b64decode(result['png']), format='png')

    @server.tool()
    async def ls(path: str = 'game', recursive: bool = False, limit: int = 200, cursor: str | None = None, studio: int | None = None) -> dict:
        """List instance hierarchy in Studio. Follow cursor for more pages."""
        return await asyncio.to_thread(environment.ls, path, recursive, limit, cursor, environment.studio_at(studio))

    @server.tool()
    async def cat(path: str, studio: int | None = None) -> dict:
        """Read reflected properties, attributes, tags, and identity of an instance in Studio."""
        return await asyncio.to_thread(environment.cat, path, environment.studio_at(studio))

    @server.tool()
    async def coverage(root: str = 'game', studio: int | None = None) -> dict:
        """Read instance/property census for the given root in Studio."""
        return await asyncio.to_thread(environment.coverage, root, environment.studio_at(studio))

    @server.tool()
    async def find(path: str = 'game', name: str | None = None, className: str | None = None, limit: int = 200, cursor: str | None = None, studio: int | None = None) -> dict:
        """Search descendants in Studio by case-insensitive name substring and IsA class."""
        return await asyncio.to_thread(environment.find, path, name, className, limit, cursor, environment.studio_at(studio))

    @server.tool()
    async def playtest(action: str = 'status', force: bool = False, studio: int | None = None) -> dict:
        """Control Play mode: action is 'status', 'start' or 'stop'. Defaults to 'status'."""
        return await asyncio.to_thread(environment.dispatch, 'playtest', {'action': action, 'force': force, 'studio': studio, 'owner': 'mcp'})
    @server.tool()
    async def runServer(code: str, timeout: float = 30, studio: int | None = None) -> dict:
        """Run Luau headlessly on the server in Run mode (StudioTestService); no Studio window required."""
        return await asyncio.to_thread(environment.run_server, code, timeout, environment.studio_at(studio))

    @server.tool()
    async def grep(query: str, plain: bool = True, ignoreCase: bool = True, limit: int = 200, skip: int = 0, studio: int | None = None) -> dict:
        """Search script source across Studio line by line (case-insensitive by default). Follow next as skip while truncated."""
        return await asyncio.to_thread(environment.grep, query, plain, ignoreCase, limit, skip, environment.studio_at(studio))
    @server.tool()
    async def libraryOptions() -> dict:
        """Read official live Creator Store categories and search filter options."""
        return await asyncio.to_thread(environment.library_options)

    @server.tool()
    async def librarySearch(query: str = '', category: str = 'Model', filters: dict | None = None):
        """Search Creator Store / Toolbox, returning thumbnail gallery and asset details."""
        import base64
        import json
        from mcp.server.fastmcp.utilities.types import Image
        result = await asyncio.to_thread(environment.library_search, query, category, filters)
        png = result.pop('png', None)
        return [json.dumps(result), Image(data=base64.b64decode(png), format='png')] if png else result

    @server.tool()
    async def libraryAdd(assetId: str, name: str | None = None, studio: int | None = None) -> dict:
        """Add public Creator Store asset in Edit mode to isolated ServerStorage.StudioMCPAssetDiscovery, disabling scripts."""
        return await asyncio.to_thread(environment.library_add, assetId, name, environment.studio_at(studio))

    @server.tool()
    async def upload(file: str, creatorId: str | None = None, creatorType: str = 'user', assetType: str | None = None, name: str | None = None, description: str = '', waitSeconds: float = 60, studio: int | None = None) -> dict:
        """Publish a local file or Studio instance path (e.g. game.Workspace.Model) via Open Cloud Assets API.
        creatorId, name, and assetType are automatically inferred if omitted.
        """
        return await asyncio.to_thread(environment.upload, file, creatorId, creatorType, assetType, name, description, waitSeconds, environment.studio_at(studio))
    async def uploadStatus(operation: str) -> dict:
        """Read completion status, asset ID, or moderation errors for an Open Cloud upload operation."""
        from .upload import operation as status
        return await asyncio.to_thread(status, operation)

    @server.tool()
    async def createGamePass(name: str, universeId: str | None = None, description: str = '', icon: str | None = None, price: int | None = None, forSale: bool | None = None, regionalPricing: bool = False) -> dict:
        """Create a permanent game pass via Open Cloud. universeId is inferred if omitted; setting price sets forSale=True."""
        if forSale is None:
            forSale = bool(price is not None)
        from .monetization import create_game_pass
        return await asyncio.to_thread(create_game_pass, universeId, name, description, icon, price, forSale, regionalPricing)

    @server.tool()
    async def createDeveloperProduct(name: str, universeId: str | None = None, description: str = '', icon: str | None = None, price: int | None = None, forSale: bool | None = None, regionalPricing: bool = False) -> dict:
        """Create a developer product via Open Cloud. universeId is inferred if omitted; setting price sets forSale=True."""
        if forSale is None:
            forSale = bool(price is not None)
        from .monetization import create_developer_product
        return await asyncio.to_thread(create_developer_product, universeId, name, description, icon, price, forSale, regionalPricing)

    @server.tool()
    async def exportAsset(path: str, output: str | None = None, studio: int | None = None) -> dict:
        """Export a Studio instance as .rbxm. If output is omitted, defaults to <instance_name>.rbxm."""
        return await asyncio.to_thread(environment.export_asset, path, output, environment.studio_at(studio))
    @server.tool()
    async def publishModel(assetId: str) -> dict:
        """Publish an existing model asset to Creator Store for free distribution."""
        from .store import publish_model
        return await asyncio.to_thread(publish_model, assetId)

    @server.tool()
    async def status() -> dict:
        """Show connected Studios, locks, running operations and recent events."""
        return await asyncio.to_thread(environment.dispatch, 'status', {})

    @server.tool()
    async def operation(id: str, wait: float = 0) -> dict:
        """Fetch result of a background operation."""
        return await asyncio.to_thread(environment.dispatch, 'operation', {'id': id, 'wait': wait})

    @server.tool()
    async def lock() -> dict:
        """Show Studio activity protection lock status."""
        return await asyncio.to_thread(environment.dispatch, 'lock', {})

    @server.tool()
    async def events(limit: int = 40) -> list[dict]:
        """Show recent events: receiver connections, playtest start/stop, lock changes."""
        return await asyncio.to_thread(environment.dispatch, 'events', {'limit': limit})

    @server.tool()
    async def profile(modules: list[str], seconds: float = 5, only: list[str] | None = None, context: str = 'client', studio: int | None = None) -> dict:
        """Profile module functions during Play: call counts, self and total ms."""
        return await asyncio.to_thread(environment.dispatch, 'profile', {'modules': modules, 'seconds': seconds, 'only': only, 'context': context, 'studio': studio})

    @server.tool()
    async def capture(frames: int = 6, interval: int = 500, state: str | None = None, context: str = 'client', studio: int | None = None) -> dict:
        """Capture a short sequence of screenshots in Play mode, optionally sampling a Luau state expression each frame."""
        return await asyncio.to_thread(environment.dispatch, 'capture', {'frames': frames, 'interval': interval, 'state': state, 'context': context, 'studio': studio})

    return server
