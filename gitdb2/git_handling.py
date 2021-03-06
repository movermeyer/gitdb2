from __future__ import print_function, division, absolute_import, \
    unicode_literals

from six import text_type

import os
import codecs
import errno

from boltons.fileutils import mkdir_p

from pygit2 import Repository, GIT_FILEMODE_BLOB, GIT_FILEMODE_TREE, \
    Signature, Oid
from pygit2 import hash as git_hash

empty_tree_id = Oid(hex='4b825dc642cb6eb9a060e54bf8d69288fbee4904')


def makedirs(dirname):
    """Creates the directories for dirname via os.makedirs, but does not raise
       an exception if the directory already exists and passes if dirname="".
    """
    if not dirname:
        return
    try:
        os.makedirs(dirname)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def remove_file_with_empty_parents(root, filename):
    """Remove root/filename, also removing any empty parents
       up to (but excluding) root"""
    root = os.path.normpath(root)
    full_filename = os.path.join(root, filename)
    if os.path.isfile(full_filename):
        os.remove(full_filename)
    parent = os.path.normpath(os.path.dirname(full_filename))
    while parent != root:
        if os.listdir(parent):
            break
        os.rmdir(parent)
        parent = os.path.normpath(os.path.dirname(parent))


def full_split(filename):
    head, tail = os.path.split(filename)
    parts = [tail]
    while head:
        head, tail = os.path.split(head)
        parts.insert(0, tail)
    return parts


def insert_into_tree(repo, tree, filename, oid, attr):
    """
    insert oid as filename into tree, possibly including subdirectories.
    Will return id of new tree.
    """
    parts = full_split(filename)
    assert len(parts) > 0
    if len(parts) > 1:
        # Create or get tree
        sub_directory = parts[0]
        sub_filename = os.path.join(*parts[1:])
        if tree is not None and sub_directory in tree:
            sub_tree = repo[tree[sub_directory].id]
        else:
            sub_tree = None

        oid = insert_blob_into_tree(repo, sub_tree, oid, sub_filename)
        mode = GIT_FILEMODE_TREE
        filename = sub_directory
    else:
        # insert in this tree
        mode = attr

    # do the actual insert
    if tree is None:
        tree_builder = repo.TreeBuilder()
    else:
        tree_builder = repo.TreeBuilder(tree)
    tree_builder.insert(filename, oid, mode)
    new_tree_id = tree_builder.write()
    return new_tree_id


def insert_blob_into_tree(repo, tree, blob_id, filename):
    """
    insert blob as filename into tree, possibly including subdirectories.
    Will return id of new tree.
    """
    return insert_into_tree(repo, tree, filename, blob_id, GIT_FILEMODE_BLOB)


def remove_file_from_tree(repo, tree, filename):
    """
    remove filename from tree, recursivly removing empty subdirectories.
    Will return id of new tree.
    """
    if tree is None:
        tree_builder = repo.TreeBuilder()
    else:
        tree_builder = repo.TreeBuilder(tree)

    parts = full_split(filename)
    assert len(parts) > 0
    if len(parts) > 1:
        sub_directory = parts[0]
        sub_filename = os.path.join(*parts[1:])
        sub_tree_entry = tree_builder.get(sub_directory)
        if not sub_tree_entry:
            return tree.id
        sub_tree = repo[sub_tree_entry.id]
        new_sub_tree_id = remove_file_from_tree(repo, sub_tree, sub_filename)

        if new_sub_tree_id == empty_tree_id:
            filename = sub_directory
        else:
            tree_builder.insert(sub_directory, new_sub_tree_id,
                                GIT_FILEMODE_TREE)
            filename = None

    # remove from this tree
    if filename and tree_builder.get(filename):
        tree_builder.remove(filename)
    new_tree_id = tree_builder.write()
    return new_tree_id


def move_file_in_tree(repo, tree, old_filename, new_filename):
    print("{} -> {}".format(old_filename, new_filename))
    tree_entry = get_tree_entry(repo, tree, old_filename)
    if not tree_entry:
        raise ValueError('filename not in tree: {}'.format(old_filename))
    oid = tree_entry.id
    filemode = tree_entry.filemode

    new_tree_id = remove_file_from_tree(repo, tree, old_filename)
    new_tree = repo[new_tree_id]
    new_tree_id = insert_into_tree(repo, new_tree, new_filename, oid, filemode)

    return new_tree_id


def get_tree_entry(repo, tree, filename):
    """Recurse through tree. If filename is in tree, returns tree entry.
    Otherwise returns None"""
    parts = full_split(filename)
    if len(parts) == 1:
        name = parts[0]
        if name in tree:
            return tree[name]
        else:
            return None
    else:
        sub_directory = parts[0]
        sub_filename = os.path.join(*parts[1:])
        if sub_directory not in tree:
            return None
        sub_tree = repo[tree[sub_directory].id]
        return get_tree_entry(repo, sub_tree, sub_filename)


class TreeModifier(object):
    """handles tree modifications of possible large scale"""
    def __init__(self, repo, tree):
        self.repo = repo
        self.tree = tree
        self.operations = []

    def insert_blob(self, blob_id, filename):
        self.operations.append(('insert', (blob_id, filename)))

    def remove_blob(self, filename):
        self.operations.append(('remove', (filename, )))

    def move(self, old_filename, new_filename):
        self.operations.append(('move', (old_filename, new_filename)))

    def simplify(self):
        """Convert list of operations into nested dictionaries of
           blob_ids to insert/remove by directory
        """
        todo = {}
        for operation, args in self.operations:
            if operation == 'insert':
                blob_id, filename = args
                todo[filename] = blob_id
            elif operation == 'remove':
                filename, = args
                todo[filename] = None
            elif operation == 'move':
                old_filename, new_filename = args
                if old_filename in todo:
                    blob_id = todo[old_filename]
                    if blob_id is None:
                        raise Exception('Trying to move deleted file',
                                        old_filename, new_filename)
                else:
                    blob_id = get_tree_entry(self.repo, self.tree,
                                             old_filename)
                    if blob_id is None:
                        raise Exception('Trying to move non existant file',
                                        old_filename, new_filename)
                todo[old_filename] = None
                todo[new_filename] = blob_id
            else:
                raise ValueError(operation)

        todo_by_directory = {}
        for filename, blob_id in todo.items():
            parts = full_split(filename)
            directories = parts[:-1]
            filename = parts[-1]
            directory = todo_by_directory
            for d in directories:
                directory = directory.setdefault(d, {})
                assert isinstance(directory, dict)
            directory[filename] = blob_id

        return todo_by_directory

    def update_tree(self, repo, tree, todo):
        """Apply nested list of new blob_ids from `simplify` to a tree
        """
        if tree is None:
            tree_builder = repo.TreeBuilder()
        else:
            tree_builder = repo.TreeBuilder(tree)

        for key, value in todo.items():
            if value is None:
                # Remove
                tree_builder.remove(key)
            elif isinstance(value, dict):
                # subdirectory
                sub_tree_entry = tree_builder.get(key)
                if not sub_tree_entry:
                    sub_tree = None
                else:
                    sub_tree = repo[sub_tree_entry.id]

                new_subtree_id = self.update_tree(repo, sub_tree, value)
                tree_builder.insert(key, new_subtree_id, GIT_FILEMODE_TREE)
            else:
                # Blob
                tree_builder.insert(key, value, GIT_FILEMODE_BLOB)
        new_tree_id = tree_builder.write()
        return new_tree_id

    def apply(self):
        todo = self.simplify()
        new_tree_id = self.update_tree(self.repo, self.tree, todo)
        new_tree = self.repo[new_tree_id]
        return new_tree


class GitHandler(object):
    def __init__(self, path, repo_path=None, update_working_copy=True):
        """
        Start a git handler in given repository.
        `update_working_copy`: wether also to update the working copy.
            By default, the git handler will only work on the git database.
            Updating the working copy can take a lot of time in
            large repositories.
        """
        self.path = path
        if repo_path is None:
            repo_path = self.path
        self.repo_path = repo_path
        self.update_working_copy = update_working_copy
        self.repo = Repository(self.repo_path)
        self.working_tree = self.get_last_tree()
        self.tree_modifier = TreeModifier(self.repo, self.working_tree)
        self.messages = []
        print("Started libgit2 git handler in ", self.path)

    def get_last_tree(self):
        if self.repo.head_is_unborn:
            tree_id = self.repo.TreeBuilder().write()
            return self.repo[tree_id]
        commit = self.repo[self.getCurrentCommit()]
        return commit.tree

    def insert_into_working_tree(self, blob_id, filename):
        self.tree_modifier.insert_blob(blob_id, filename)

    def remove_from_working_tree(self, filename):
        self.tree_modifier.remove_blob(filename)

    def write_file(self, filename, content):
        # TODO: combine writing many files
        assert isinstance(content, text_type)
        data = content.encode('utf-8')
        existing_entry = get_tree_entry(self.repo, self.working_tree, filename)
        if existing_entry:
            type = 'M'
            if existing_entry.id == git_hash(data):
                return
        else:
            type = 'A'
        blob_id = self.repo.create_blob(data)
        self.insert_into_working_tree(blob_id, filename)

        if not self.repo.is_bare and self.update_working_copy:
            real_filename = os.path.join(self.path, filename)
            mkdir_p(os.path.dirname(real_filename))
            with codecs.open(real_filename, 'w', encoding='utf-8') as outfile:
                outfile.write(content)

        self.messages.append('    {}  {}'.format(type, filename))

    def remove_file(self, filename):
        existing_entry = get_tree_entry(self.repo, self.working_tree, filename)
        if existing_entry:
            self.remove_from_working_tree(filename)

            if not self.repo.is_bare and self.update_working_copy:
                remove_file_with_empty_parents(self.path, filename)

            self.messages.append('    D  {}'.format(filename))

    def move_file(self, old_filename, new_filename):
        self.tree_modifier.move(old_filename, new_filename)

        if not self.repo.is_bare and self.update_working_copy:
            real_old_filename = os.path.join(self.path, old_filename)
            real_new_filename = os.path.join(self.path, new_filename)
            mkdir_p(os.path.dirname(real_new_filename))
            os.rename(real_old_filename, real_new_filename)
            remove_file_with_empty_parents(self.path, old_filename)

        self.messages.append('    R  {} -> {}'.format(old_filename,
                                                      new_filename))

    def commit(self):
        if self.tree_modifier.tree.oid != self.get_last_tree().oid:
            raise Exception("The repository was modified outside of this process. For safety reasons, we cannot commit!")
        self.working_tree = self.tree_modifier.apply()
        self.tree_modifier = TreeModifier(self.repo, self.working_tree)

        if self.repo.head_is_unborn:
            parents = []
        else:
            commit = self.repo[self.getCurrentCommit()]
            if commit.tree.id == self.working_tree.id:
                return
            parents = [commit.id]

        config = self.repo.config
        author = Signature(config['user.name'], config['user.email'])
        committer = Signature(config['user.name'], config['user.email'])
        tree_id = self.working_tree.id
        message = '\n'.join(self.messages)
        self.repo.create_commit('refs/heads/master',
                                author, committer, message,
                                tree_id,
                                parents)
        self.saveCurrentCommit()
        self.messages = []
        if not self.repo.is_bare and self.update_working_copy:
            self.repo.index.read_tree(self.working_tree)
            self.repo.index.write()

    def reset(self):
        self.working_tree = self.get_last_tree()
        self.tree_modifier = TreeModifier(self.repo, self.working_tree)
        self.messages = []

    def getCurrentCommit(self):
        return self.repo.head.target

    def saveCurrentCommit(self):
        with open(os.path.join(self.path, 'dbcommit'), 'w') as dbcommit_file:
            dbcommit_file.write(self.getCurrentCommit().hex+'\n')
