#!/usr/bin/env python3
"""
Script de prueba para el servidor MCP de Notion.
"""

import os
import sys
import json
from dotenv import load_dotenv

# Agregar el directorio actual al path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

def test_notion_client():
    """Prueba básica del cliente de Notion."""
    try:
        from notion_client import Client
        
        api_key = os.environ.get("NOTION_API_KEY")
        if not api_key:
            print("❌ ERROR: NOTION_API_KEY no configurada en .env")
            print("   Configura NOTION_API_KEY=secret_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
            return False
        
        print(f"🔑 API Key: {api_key[:10]}...{api_key[-4:]}")
        
        # Crear cliente
        client = Client(auth=api_key)
        
        # Probar conexión obteniendo información del bot
        print("🔌 Probando conexión a Notion API...")
        try:
            bot_info = client.users.me()
            print(f"✅ Conexión exitosa")
            print(f"   Bot ID: {bot_info.get('id')}")
            print(f"   Bot Name: {bot_info.get('name')}")
            print(f"   Bot Type: {bot_info.get('type')}")
            
            # Buscar algunas bases de datos
            print("\n🔍 Buscando bases de datos...")
            search_result = client.search(
                filter={"property": "object", "value": "database"},
                page_size=3
            )
            
            databases = search_result.get("results", [])
            print(f"   Encontradas {len(databases)} bases de datos")
            
            for i, db in enumerate(databases[:3]):
                db_id = db.get("id", "N/A")
                db_title = "Sin título"
                if "title" in db and db["title"]:
                    title_text = db["title"]
                    if isinstance(title_text, list) and title_text:
                        db_title = ''.join([t.get('plain_text', '') for t in title_text])
                    else:
                        db_title = str(title_text)
                
                print(f"   {i+1}. {db_title[:50]}... (ID: {db_id[:8]}...)")
            
            # Buscar algunas páginas
            print("\n🔍 Buscando páginas...")
            page_result = client.search(
                filter={"property": "object", "value": "page"},
                page_size=3
            )
            
            pages = page_result.get("results", [])
            print(f"   Encontradas {len(pages)} páginas")
            
            for i, page in enumerate(pages[:3]):
                page_id = page.get("id", "N/A")
                page_url = page.get("url", "N/A")
                print(f"   {i+1}. Página (ID: {page_id[:8]}..., URL: {page_url[:50]}...)")
            
            return True
            
        except Exception as e:
            print(f"❌ Error de API: {e}")
            print("\n📋 Posibles soluciones:")
            print("   1. Verifica que la API key sea correcta")
            print("   2. Asegúrate de que la integración tenga acceso a páginas/bases de datos")
            print("   3. Verifica tu conexión a internet")
            return False
            
    except ImportError as e:
        print(f"❌ Error de importación: {e}")
        print("\n📋 Instala las dependencias:")
        print("   pip install notion-client python-dotenv")
        return False
    except Exception as e:
        print(f"❌ Error inesperado: {e}")
        return False

def test_mcp_server():
    """Prueba el servidor MCP directamente."""
    print("\n" + "="*60)
    print("🧪 Probando servidor MCP de Notion...")
    print("="*60)
    
    try:
        # Importar y ejecutar el servidor en modo test
        from notion_mcp_server import cli_test
        return cli_test() == 0
    except Exception as e:
        print(f"❌ Error al probar servidor MCP: {e}")
        return False

def main():
    print("🚀 Iniciando pruebas del servidor MCP de Notion")
    print("="*60)
    
    # Verificar archivos necesarios
    required_files = [
        "notion_mcp_server.py",
        ".env",
        "requirements.txt"
    ]
    
    print("\n📁 Verificando archivos...")
    for file in required_files:
        if os.path.exists(file):
            print(f"   ✅ {file}")
        else:
            if file == ".env":
                print(f"   ⚠️  {file} (crea copiando .env.notion.example)")
            else:
                print(f"   ❌ {file} (no encontrado)")
    
    # Probar cliente de Notion
    print("\n" + "="*60)
    client_ok = test_notion_client()
    
    if client_ok:
        # Probar servidor MCP
        print("\n" + "="*60)
        mcp_ok = test_mcp_server()
        
        if mcp_ok:
            print("\n" + "="*60)
            print("🎉 ¡Todas las pruebas pasaron!")
            print("\n📋 Pasos siguientes:")
            print("   1. Configura tu .env con tu API key real")
            print("   2. Actualiza claude_desktop_config.json con tu API key")
            print("   3. Reinicia Claude Desktop")
            print("   4. Usa herramientas como:")
            print("      - 'Busca páginas sobre X en Notion'")
            print("      - 'Obtén la página con ID X'")
            print("      - 'Consulta la base de datos X'")
            return 0
        else:
            print("\n❌ El servidor MCP falló")
            return 1
    else:
        print("\n❌ El cliente de Notion falló")
        return 1

if __name__ == "__main__":
    sys.exit(main())