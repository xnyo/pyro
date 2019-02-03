import glob
import multiprocessing
import os
import shutil
import subprocess
import sys
import time

from collections import OrderedDict

try:
    from lxml import etree
except ImportError:
    subprocess.call([sys.executable, '-m', 'pip', 'install', 'lxml'])
    # noinspection PyUnresolvedReferences
    from lxml import etree

from Arguments import Arguments
from Logger import Logger
from Project import Project
from TimeElapsed import TimeElapsed


class PapyrusProject:
    log = Logger()

    def __init__(self, prj: Project):
        self.project = prj
        self.compiler_path = prj.get_compiler_path()
        self.game_path = prj.get_game_path()
        self.game_type = prj.game_type
        self.input_path = prj.input_path

        self.root_node = etree.parse(prj.input_path, etree.XMLParser(remove_blank_text=True)).getroot()
        self.output_path = self.root_node.get('Output')
        self.flags_path = self.root_node.get('Flags')

    @staticmethod
    def _get_node(parent_node: etree.Element, tag: str, ns: str = 'PapyrusProject.xsd') -> etree.Element:
        return parent_node.find('ns:%s' % tag, {'ns': '%s' % ns})

    @staticmethod
    def _get_node_children(parent_node: etree.Element, tag: str, ns: str = 'PapyrusProject.xsd') -> list:
        return parent_node.findall('ns:%s' % tag[:-1], {'ns': '%s' % ns})

    @staticmethod
    def _get_node_children_values(parent_node: etree.Element, tag: str) -> list:
        node = PapyrusProject._get_node(parent_node, tag)

        if node is None:
            exit(PapyrusProject.log.pyro('The PPJ file is missing the following tag: {0}'.format(tag)))

        child_nodes = PapyrusProject._get_node_children(node, tag)

        if child_nodes is None or len(child_nodes) == 0:
            sys.tracebacklimit = 0
            raise Exception('No child nodes exist for <%s> tag' % tag)

        return [str(field.text) for field in child_nodes if field.text is not None and field.text != '']

    @staticmethod
    def _open_process(command: str, use_bsarch: bool = False) -> int:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, universal_newlines=True)

        exclusions = ('Starting', 'Assembly', 'Compilation', 'Batch', 'Copyright', 'Papyrus', 'Failed', 'No output')

        try:
            while process.poll() is None:
                line = process.stdout.readline().strip()
                if not use_bsarch:
                    exclude_lines = not line.startswith(exclusions)
                    PapyrusProject.log.compiler(line) if line != '' and exclude_lines and 'error(s)' not in line else None
                else:
                    PapyrusProject.log.bsarch(line) if line != '' else None
            return 0

        except KeyboardInterrupt:
            try:
                process.terminate()
            except OSError:
                pass
            return 0

    @staticmethod
    def _unique_list(items: list) -> list:
        return list(OrderedDict.fromkeys(items))

    def _build_commands(self, quiet: bool) -> list:
        commands = list()

        unique_imports = self._get_imports_from_script_paths()
        script_paths = self._get_script_paths()

        arguments = Arguments()

        for script_path in script_paths:
            arguments.clear()
            arguments.append_quoted(self.compiler_path)
            arguments.append_quoted(script_path)
            arguments.append_quoted(self.output_path, 'o')
            arguments.append_quoted(';'.join(unique_imports), 'i')
            arguments.append_quoted(self.flags_path, 'f')

            if self.project.is_fallout4:
                release = self.root_node.get('Release')
                if release and release.casefold() == 'true':
                    arguments.append('-release')

                final = self.root_node.get('Final')
                if final and final.casefold() == 'true':
                    arguments.append('-final')

            optimize = self.root_node.get('Optimize')
            if optimize and optimize.casefold() == 'true':
                arguments.append('-op')

            if quiet:
                arguments.append('-q')

            commands.append(arguments.join())

        return commands

    def _build_commands_native(self, quiet: bool) -> str:
        arguments = Arguments()
        arguments.append_quoted(os.path.join(self.game_path, self.compiler_path))
        arguments.append_quoted(self.input_path)

        if quiet:
            arguments.append('-q')

        return arguments.join()

    def _build_commands_bsarch(self, script_folder: str, archive_path: str) -> str:
        bsarch_path = self.project.get_bsarch_path()

        arguments = Arguments()

        arguments.append_quoted(bsarch_path)
        arguments.append('pack')
        arguments.append_quoted(script_folder)
        arguments.append_quoted(archive_path)

        if self.project.is_fallout4:
            arguments.append('-fo4')
        elif self.project.is_skyrim_special_edition:
            arguments.append('-sse')
        else:
            arguments.append('tes5')

        return arguments.join()

    def _get_imports_from_script_paths(self) -> list:
        """Generate list of unique import paths from script paths"""
        script_paths = self._get_script_paths()

        xml_import_paths = self._get_node_children_values(self.root_node, 'Imports')

        script_import_paths = list()

        for script_path in script_paths:
            for xml_import_path in xml_import_paths:
                test_path = os.path.join(xml_import_path, os.path.dirname(script_path))

                if os.path.exists(test_path):
                    script_import_paths.append(test_path)

        return self._unique_list(script_import_paths + xml_import_paths)

    def _copy_scripts_to_temp_path(self, script_paths: list, tmp_scripts_path: str) -> None:
        output_path = self.output_path

        if any(dots in output_path.split(os.sep) for dots in ['.', '..']):
            output_path = os.path.normpath(os.path.join(os.path.dirname(self.input_path), output_path))

        compiled_script_paths = map(lambda x: os.path.join(output_path, x.replace('.psc', '.pex')), script_paths)

        if not self.project.is_fallout4:
            compiled_script_paths = map(lambda x: os.path.join(output_path, os.path.basename(x)), compiled_script_paths)

        for compiled_script_path in compiled_script_paths:
            abs_compiled_script_path = os.path.abspath(compiled_script_path)
            tmp_destination_path = os.path.join(tmp_scripts_path, os.path.basename(abs_compiled_script_path))

            shutil.copy2(abs_compiled_script_path, tmp_destination_path)

    def _get_script_paths(self) -> list:
        """Retrieves script paths both Folders and Scripts nodes"""
        paths = list()

        # <Folders>
        folder_paths = self._get_script_paths_from_folders_node()
        if len(folder_paths) > 0:
            paths.extend(folder_paths)

        script_paths = self._get_script_paths_from_scripts_node()
        if len(script_paths) > 0:
            paths.extend(script_paths)

        norm_paths = map(lambda x: os.path.normpath(x), paths)

        return self._unique_list(list(norm_paths))

    def _get_script_paths_from_folders_node(self) -> list:
        """Retrieves script paths from the Folders node"""
        script_paths = list()

        folders_node = self._get_node(self.root_node, 'Folders')

        if folders_node is not None:
            # defaults to False if the attribute does not exist
            no_recurse = bool(folders_node.get('NoRecurse'))

            for folder in self._get_node_children_values(self.root_node, 'Folders'):
                # fix relative paths
                if folder == '..' or folder == '.':
                    folder = os.path.abspath(os.path.join(os.path.dirname(self.input_path), folder))
                elif not os.path.isabs(folder):
                    # try to find folder in import paths
                    for import_path in self._get_node_children_values(self.root_node, 'Imports'):
                        test_path = os.path.join(import_path, folder)

                        if os.path.exists(test_path):
                            folder = test_path
                            break

                abs_script_paths = glob.glob(os.path.join(folder, '*.psc'), recursive=not no_recurse)

                # we need path parts, not absolute paths - we're assuming namespaces though (critical flaw?)
                for script_path in abs_script_paths:
                    namespace, file_name = map(lambda x: os.path.basename(x), [os.path.dirname(script_path), script_path])
                    script_paths.append(os.path.join(namespace, file_name))

        return script_paths

    def _get_script_paths_from_scripts_node(self) -> list:
        """Retrieves script paths from the Scripts node"""
        script_paths = list()

        scripts_node = self._get_node(self.root_node, 'Scripts')
        if scripts_node is not None:
            # "support" colons by replacing them with path separators so they're proper path parts
            # but watch out for absolute paths and use the path parts directly instead
            def fix_path(script_path):
                if os.path.isabs(script_path):
                    namespace, file_name = map(lambda x: os.path.basename(x), [os.path.dirname(script_path), script_path])
                    return os.path.join(namespace, file_name)
                return script_path.replace(':', os.sep)

            scripts = map(lambda x: fix_path(x), self._get_node_children_values(self.root_node, 'Scripts'))

            script_paths.extend(scripts)

        return script_paths

    def _parallelize(self, commands: list) -> None:
        p = multiprocessing.Pool(processes=os.cpu_count())
        p.map(self._open_process, commands)
        p.close()
        p.join()

    def compile_native(self, quiet: bool, time_elapsed: TimeElapsed) -> None:
        commands = self._build_commands_native(quiet)
        time_elapsed.start_time = time.time()
        self._open_process(commands)
        time_elapsed.end_time = time.time()

    def compile_custom(self, quiet: bool, time_elapsed: TimeElapsed) -> None:
        commands = self._build_commands(quiet)
        time_elapsed.start_time = time.time()
        self._parallelize(commands)
        time_elapsed.end_time = time.time()

    def pack_archive(self) -> None:
        # create temporary folder
        tmp_path = os.path.normpath(os.path.join(os.path.dirname(__file__), self.project._ini['Shared']['TempPath']))
        tmp_scripts_path = os.path.join(tmp_path, 'Scripts')

        # clear temporary data
        if os.path.exists(tmp_path):
            shutil.rmtree(tmp_path)

        # ensure temporary data paths exist
        if not os.path.exists(tmp_scripts_path):
            os.makedirs(tmp_scripts_path)

        script_paths = self._get_script_paths()

        self._copy_scripts_to_temp_path(script_paths, tmp_scripts_path)

        archive_path = self.root_node.get('Archive')

        if archive_path is None:
            return PapyrusProject.log.error('Cannot pack archive because Archive attribute not set')

        commands = self._build_commands_bsarch(*map(lambda x: os.path.normpath(x), [tmp_path, archive_path]))

        self._open_process(commands, use_bsarch=True)

        # clear temporary data
        if os.path.exists(tmp_path):
            shutil.rmtree(tmp_path)

    def validate_project(self, time_elapsed: TimeElapsed) -> None:
        script_paths = self._get_script_paths()

        output_path = self.output_path

        if any(dots in output_path.split(os.sep) for dots in ['.', '..']):
            output_path = os.path.join(os.path.dirname(self.input_path), output_path)

        compiled_script_paths = map(lambda x: os.path.join(output_path, x.replace('.psc', '.pex')), script_paths)

        if not self.project.is_fallout4:
            compiled_script_paths = map(lambda x: os.path.join(output_path, os.path.basename(x)), compiled_script_paths)

        for compiled_script_path in compiled_script_paths:
            abs_compiled_script_path = os.path.abspath(compiled_script_path)
            self.project.validate_script(abs_compiled_script_path, time_elapsed)