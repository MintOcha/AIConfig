"""Bounded concurrent file transfers with native Kaggle publication and filtering."""
from concurrent.futures import ThreadPoolExecutor
from kaggle.api.kaggle_api_extended import KaggleApi, ResumableUploadContext


class DatasetApi(KaggleApi):
    def upload_files(self, request, resources, folder, blob_type, upload_context,
                     quiet=False, dir_mode="skip", ignore_patterns=None):
        pending = []

        class Inventory(KaggleApi):
            def _upload_file_or_folder(self, *args):
                pending.append(args)
                return None

        Inventory().upload_files(request, resources, folder, blob_type, upload_context,
                                 quiet, dir_mode, ignore_patterns)
        if not pending:
            return

        def transfer(args):
            api = KaggleApi()
            api.authenticate()
            with ResumableUploadContext() as context:
                args = list(args)
                args[3] = context
                args[5] = True
                return api._upload_file_or_folder(*args)

        with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
            for uploaded in pool.map(transfer, pending):
                if uploaded is None:
                    raise RuntimeError("A dataset file failed to upload; publication aborted")
                request.files.append(self._new_file(uploaded))
