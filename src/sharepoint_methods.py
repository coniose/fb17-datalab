from office365.runtime.auth.authentication_context import AuthenticationContext
from office365.sharepoint.client_context import ClientContext
from office365.sharepoint.listitems.collection import ListItemCollection
from office365.sharepoint.files.file import File
from pandas.tseries.offsets import DateOffset
from datetime import datetime
import pandas as pd
import numpy as np
import unicodedata
import requests
import tempfile
import io
import os
import re

class SharePointClient:
    def __init__(self, url, username, password):
        self.url = url
        self.username = username
        self.password = password
        self.ctx = self.authenticate()
        self.web = self.ctx.web
        self._email_cache: dict = {}

    def authenticate(self):
        ctx_auth = AuthenticationContext(self.url)
        if ctx_auth.acquire_token_for_user(self.username, self.password):
            return ClientContext(self.url, ctx_auth)
        else:
            raise Exception("Authentication failed: " + ctx_auth.get_last_error())

    def exclude_system_columns(self, list_name):
        system_columns = {
            "FileSystemObjectType",
            "ServerRedirectedEmbedUri",
            "ServerRedirectedEmbedUrl",
            "ContentTypeId",
            "OData__ColorTag",
            "ComplianceAssetId",
            "OData__UIVersionString",
            "GUID",
            "AuthorId",
            "EditorId",
            "Attachments",
        }

        target_list = self.web.lists.get_by_title(list_name)
        fields = target_list.fields.get().execute_query()
        col_names = [
            f.internal_name
            for f in fields
            if not f.hidden and f.internal_name not in system_columns
        ]
        return col_names

    def query_large_list(self, list_name) -> pd.DataFrame:
        target_list = self.web.lists.get_by_title(list_name)
        column_names = self.exclude_system_columns(list_name)
        paged_items = target_list.items.paged(5000, page_loaded=self.print_progress).get().execute_query()
        data = {cl: [item.properties.get(cl) for item in paged_items] for cl in column_names}
        return pd.DataFrame(data)

    def query_large_list_limited(self, list_name, max_rows: int) -> pd.DataFrame:
        target_list = self.web.lists.get_by_title(list_name)
        column_names = self.exclude_system_columns(list_name)
    
        paged_items = []
        items_paged = target_list.items.paged(5000, page_loaded=self.print_progress).get().execute_query()
    
        for item in items_paged:
            paged_items.append(item)
            if len(paged_items) >= max_rows:
                break
    
        data = {cl: [item.properties.get(cl) for item in paged_items] for cl in column_names}
        return pd.DataFrame(data)


    def get_person_bad(self, user_param):
        return self.web.site_users.get_by_id(user_param).get().execute_query().properties['Email']

    def get_person(self, user_id):
        # Cache por instância (self._email_cache) — não usar default mutável
        # como argumento, que vira cache global compartilhado entre instâncias.
        if user_id in self._email_cache:
            return self._email_cache[user_id]
        try:
            user_email = self.web.site_users.get_by_id(user_id).get().execute_query().properties['Email']
            self._email_cache[user_id] = user_email
            return user_email
        except Exception as e:
            # print(f"Error fetching email for user ID {user_id}: {e}")
            return None

    def list_to_dataframe(self, list_name):
        # query_large_list aceita apenas (list_name) — passar column_names
        # lançava TypeError. A exclusão de colunas de sistema é responsabilidade
        # do chamador, se necessária.
        return self.query_large_list(list_name)

    def download_and_read_excel(self, relative_url):
        import warnings
        response = File.open_binary(self.ctx, relative_url)
        byte_stream = io.BytesIO(response.content)
        xls = pd.ExcelFile(byte_stream)
        sheet_names = xls.sheet_names

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
            if len(sheet_names) > 1:
                return [pd.read_excel(xls, sheet_name=sheet) for sheet in sheet_names]
            else:
                return [pd.read_excel(xls, sheet_name=sheet_names[0])]


    def download_binary_file(self, relative_url):
        file = self.web.get_file_by_server_relative_url(relative_url)
        self.ctx.load(file)
        self.ctx.execute_query()
        response = File.open_binary(self.ctx, relative_url)
        
        byte_content = io.BytesIO(response.content)
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file.write(byte_content.getvalue())
            temp_file_path = temp_file.name
        
        return temp_file_path

    def upload_csv_file(self, csv_file, target_url):
        local_path = csv_file

        def print_upload_progress(offset):
            file_size = os.path.getsize(local_path)
            print(f"Uploaded '{offset}' bytes from '{file_size}'...[{round(offset / file_size * 100, 2)}%]")
        
        target_folder = self.web.get_folder_by_server_relative_url(target_url)
        size_chunk = 1000000

        with open(local_path, "rb") as f:
            uploaded_file = target_folder.files.create_upload_session(f, size_chunk, print_upload_progress).execute_query()

        print(f"File {uploaded_file.serverRelativeUrl} has been uploaded successfully.")

    def insert_list_item(self, list_name, item_dict_list: list) -> list:
        """Insere itens e retorna lista de IDs criados (int ou None se não disponível)."""
        sp_list = self.web.lists.get_by_title(list_name)
        adding = [sp_list.add_item(item_dict) for item_dict in item_dict_list]
        self.ctx.execute_query()
        print("{0} items created".format(len(adding)))
        return [item.properties.get("ID") for item in adding]

    def update_list_items(self, list_name, id_list, target_column, new_value):
        sp_list = self.web.lists.get_by_title(list_name)
        filter_query = " or ".join([f"ID eq {id}" for id in id_list])
        items = sp_list.items.get().filter(filter_query).execute_query()
        [item.set_property(target_column, new_value).update() for item in items]
        sp_list.execute_batch()

    def delete_list_items(self, list_name, id_list):
        sp_list = self.web.lists.get_by_title(list_name)
        filter_query = " or ".join([f"ID eq {id}" for id in id_list])
        items = sp_list.items.get().filter(filter_query).execute_query()
        [item.delete_object() for item in items]
        sp_list.execute_batch()

    def list_all_lists(self) -> list:
        lists = self.web.lists.get().execute_query()
        list_titles = [sp_list.properties['Title'] for sp_list in lists]
        return list_titles

    def list_all_files(self, folder_url):
        """
        List all files in a given SharePoint folder.
        
        :param folder_url: The server-relative URL of the folder.
        :return: A list of file names.
        """
        folder = self.web.get_folder_by_server_relative_url(folder_url)
        self.ctx.load(folder)
        self.ctx.execute_query()

        files = folder.files
        self.ctx.load(files)
        self.ctx.execute_query()

        file_names = [file.properties['Name'] for file in files]
        return file_names
        # Example usage:
        # Assuming you have already created an instance of SharePointClient
        # sharepoint_client = SharePointClient(url, username, password)
        # files = sharepoint_client.list_all_files("/sites/YourSite/Shared Documents/YourFolder")
        # print(files)

    def list_all_folders(self, folder_url):
        """
        List all folders in a given SharePoint folder.
        
        :param folder_url: The server-relative URL of the folder.
        :return: A list of folder names.
        """
        folder = self.web.get_folder_by_server_relative_url(folder_url)
        self.ctx.load(folder)
        self.ctx.execute_query()

        folders = folder.folders
        self.ctx.load(folders)
        self.ctx.execute_query()

        folder_names = [folder.properties['Name'] for folder in folders]
        return folder_names
        # Example usage:
        # Assuming you have already created an instance of SharePointClient
        # sharepoint_client = SharePointClient(url, username, password)
        # folders = sharepoint_client.list_all_folders("/sites/YourSite/Shared Documents/YourFolder")
        # print(folders)
   

    def get_column_names(self, list_name) -> dict:
        """
        Retorna um dicionário com os nomes internos e legíveis das colunas de uma lista SharePoint.
        
        Parâmetros:
            web: objeto de conexão com o site SharePoint (ClientContext().web)
            list_name: nome da lista SharePoint (string)
        
        Retorno:
            dict: {internal_name: display_name}
        """
        target_list = self.web.lists.get_by_title(list_name)
        fields = target_list.fields.get().execute_query()

        column_map = {
            field.internal_name: field.title
            for field in fields
            if not field.hidden and not field.read_only_field
        }
        
        system_columns = [
            "FileSystemObjectType",
            "ServerRedirectedEmbedUri",
            "ServerRedirectedEmbedUrl",
            "ContentTypeId",
            "OData__ColorTag",
            "ComplianceAssetId",
            "OData__UIVersionString",
            "GUID",
            "AuthorId",	
            "EditorId",
            "Attachments"
        ]
        return {k: v for k, v in column_map.items() if k not in system_columns}

    @staticmethod
    def print_progress(items):
        #print(items)
        pass


# How to connect

# import os
# from dotenv import load_dotenv
# from methods.sharepoint_methods import SharePointClient
# import pandas as pd
# import re

