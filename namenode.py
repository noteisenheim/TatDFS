from flask import Flask, Response, request, jsonify
import logging
from anytree import Node, RenderTree, Resolver
from datetime import datetime
from threading import Thread
import os, requests, random, time
from FileSystem import fs

ROOT_DIR = "root"
HOST = '0.0.0.0'
PORT = 8080
DATANODES = ["http://0.0.0.0:8085"]
# DATANODES = ["http://0.0.0.0:8085", "http://0.0.0.0:8086", "http://0.0.0.0:8087"]
HEARTBEAT_RATE = 5

app = Flask(__name__)
logging.basicConfig(filename='namenode.log', level=logging.DEBUG)


def heartbeat():
    while True:
        print("started heartbeat")
        # go through each datanode
        new_alive = []  # dead -> alive
        new_dead = []  # alive -> dead

        print(f"live_datanodes: {fs.live_datanodes}")
        print(f"dead datanodes: {fs.dead_datanodes}")

        # updating new_dead
        for cur_node in fs.live_datanodes:
            try:
                response = requests.get(cur_node + '/ping')  # pinging current datanode
                if response.status_code // 100 != 2:
                    new_dead.append(cur_node)
                    print(f"new dead: {cur_node}")
            except Exception as e:
                new_dead.append(cur_node)
                print(f"new dead: {cur_node}")

        # updating new_alive
        for cur_node in fs.dead_datanodes:
            try:
                response = requests.get(cur_node + '/ping')
                if response.status_code // 100 == 2:
                    print(f"new alive: {cur_node}")
                    new_alive.append(cur_node)
            except Exception as e:
                print(f"FAILED to PING datanode {cur_node}")

        # resurrecting nodes
        for node in new_alive:
            # request the datanode to format
            response = requests.get(node + '/format')

            if response.status_code // 100 != 2:
                print(f"couldn't resurrect datanode: {node}")
            else:
                fs.live_datanodes.append(node)
                fs.dead_datanodes.remove(node)
                fs.datanodes_files[node] = []

        # getting the up to date list of live and dead datanodes
        for node in new_dead:
            fs.live_datanodes.remove(node)
            fs.dead_datanodes.append(node)

        # replicating files from dead datanodes
        for node in new_dead:
            print(f"replica on dead for {node}")
            fs.replicate_on_dead(node)

        print(f"needs_replica: {fs.needs_replica}")
        needs_repl = []
        # try to replicate files that needs replication
        for node in fs.needs_replica.keys():
            file = node.file
            needed = fs.replication - len(file['datanodes'])
            new_datanodes = fs.choose_datanodes(n=needed, exclude=file['datanodes'])
            for new_datanode in new_datanodes:
                for datanode in file['datanodes']:
                    print(f"started replicating from {datanode}")
                    try:
                        response = requests.post(new_datanode + '/get-replica',
                                                 json={'file_id': file['id'], 'datanode': datanode})
                    except Exception as e:
                        print("couldn't replicate")
                        continue

                    if response.status_code // 100 == 2:
                        if new_datanode in fs.datanodes_files.keys():
                            fs.datanodes_files[new_datanode].append(file['id'])
                        else:
                            fs.datanodes_files[new_datanode] = [file['id']]
                        print(f"file was replicated")
                        file['datanodes'] += [new_datanode]
                        break
                    else:
                        print(f"file was NOT replicated")
            node.file = file
            needs_repl.append(node)

        for node in needs_repl:
            fs.update_needs_replica(node, remove=False)

        print(f"needs replica: {fs.needs_replica}")

        time.sleep(HEARTBEAT_RATE)


@app.route('/ping')
def ping():
    return Response("ping from namenode", 200)


@app.route('/curdir')
def curdir():
    return jsonify({'current_dir': fs.get_current_dirname()})


@app.route('/init')
def init():
    print("starting INIT in NAMENODE")

    # initialize FS
    fs.__init__()
    live_datanodes = []
    dead_datanodes = []

    # check whether nodes are alive
    # if yes format them
    for datanode in DATANODES:
        # ping datanode
        try:
            response = requests.get(datanode + '/ping')
        except Exception as e:
            print(f"couldn't ping DATANODE {datanode} because of\n{e}")
            # update dead datanodes
            dead_datanodes.append(datanode)
            continue

        # if ok
        if response.status_code // 100 == 2:
            # formatting datanode
            try:
                response = requests.get(datanode + '/format')
            except Exception as e:
                print(f"couldn't FORMAT DATANODE {datanode} because of\n{e}\nAppending to dead datanodes")
                dead_datanodes.append(datanode)
                continue

            if response.status_code // 100 != 2:
                print(f"couldn't FORMAT DATANODE {datanode}\nAppending to dead datanodes")
                dead_datanodes.append(datanode)
                app.logger.info(f"couldn't FORMAT DATANODE {datanode}")

            else:
                # updating free space
                spaces = response.json()
                free = spaces['free']
                fs.free_space = min(free, fs.free_space)
                # updating live datanodes
                live_datanodes.append(datanode)

        else:
            print(f"couldn't ping DATANODE {datanode}")
            # app.logger.info(f"couldn't pind DATANODE {datanode}")

    # check whether the FS initialized successfully
    app.logger.info("checking len of live_datanodes")
    print(f"LIVE DATANODES: {live_datanodes}")
    print(f"DEAD DATANODES: {dead_datanodes}")
    fs.dead_datanodes = dead_datanodes
    fs.live_datanodes = live_datanodes
    if len(live_datanodes) > 0:
        app.logger.info(f"live datanodes: {live_datanodes}")
        return jsonify({"free_space": fs.free_space})
    else:
        return Response("FAILED: couldn't INIT as no live datanodes", 418)


@app.route('/delete', methods=['DELETE'])
def delete():
    # delete file from FS
    print("starting deleting file")
    filename = request.json['filename']

    print(f"filename = {filename}")

    # try to obtain file from the filesystem
    node = fs.get_file(filename)
    if node:
        print("file exists")
        file = fs.delete_file(node)
        print(f"file = {file}")
        return jsonify({"file": file})
    else:
        print("file doesn't exist")
        return Response("FAILED: file doesn't exist", 404)


@app.route('/delete/dir-notsure', methods=['DELETE'])
def delete_dir_notsure():
    print("starting deleting dir")
    dirname = request.json['dirname']

    print(f"dirname: {dirname}")

    # try to obtain directory from the filesystem
    dir_node = fs.get_dir(dirname)
    if dir_node:
        children = [x for x in dir_node.children]
        # check if directory contains something
        if len(children) == 0:
            dir_node.parent = None
            print("directory empty, was removed successfully")
            return jsonify({"empty": True})
        else:
            print("directory not empty")
            return jsonify({"empty": False})
    else:
        print("dir doesn't exist")
        return Response("FAILED: dir doesn't exist", 404)


@app.route('/delete/dir-sure', methods=['DELETE'])
def delete_dir_sure():
    print("starting deleting dir")
    dirname = request.json['dirname']

    print(f"dirname: {dirname}")

    # obtain the directory from the filesystem
    dir_node = fs.get_dir(dirname)
    if dir_node:
        # get all files in the specified directory
        files = fs.get_all_files_rec(dir_node)
        # remove directory and all its children from the filesystem
        dir_node.parent = None
        return jsonify({"files": files})

    else:
        print("dir doesn't exist")
        return Response("FAILED: dir doesn't exist", 404)


@app.route('/copy', methods=['POST'])
def copy():
    print("started copying file in namenode")
    filename = request.json['filename']
    dirname = request.json['dirname']

    # node with the file
    original_node = fs.get_file(filename)
    if original_node:
        print(f"file {filename} found")
        # node with the new dir
        if dirname[-1] == '/':
            new_node_par = fs.get_dir(dirname)
            if new_node_par:
                # resolving name collision
                new_name = os.path.basename(filename) + '_copy'
                count = 1
                file = fs.get_file(new_name)
                while file:
                    new_name = new_name + str(count)
                    count += 1
                    file = fs.get_file(new_name)
                # creating copy of the file
                file = fs.create_file(new_name, new_node_par, original_node.file['size'])
                print(f"file was copied under the filename {filename}")
                return jsonify({'original': original_node.file, 'copy': file})
        else:
            dir_name = os.path.dirname(dirname)
            file_name = os.path.basename(dirname)
            file = fs.get_file(dirname)
            if file or fs.get_dir(dirname):
                print("specified file already exists")
                return Response("FAILED: specified file already exists", 404)
            else:
                parent_dir = fs.get_dir(dir_name)
                if parent_dir:
                    file = fs.create_file(file_name, parent_dir, original_node.file['size'])
                    print(f"file was copied under the filename {filename}")
                    return jsonify({'original': original_node.file, 'copy': file})
                else:
                    print("specified directory does not exist")
                    return Response("FAILED: specified directory does not exist", 404)
    else:
        return Response("FAILED: file doesn't exist", 404)


@app.route('/get', methods=['GET'])
def get():
    print("started GETTING the file in NAMENODE")
    filename = request.json['filename']
    print(f"filename = {filename}")

    file = fs.get_file(filename)
    if file:
        print("file exists")
        print(f"file = {file.file}")
        return jsonify({"file": file.file})

    else:
        print("file doesn't exist")
        return Response("FAILED: file doesn't exist", 404)


@app.route('/create', methods=['POST'])
def create():
    print("started CREATING file")
    # obtain filename
    filename = request.json['filename']
    filesize = 0
    if request.json['filesize']:
        filesize = request.json['filesize']

    # check whether file already exists
    if fs.get_file(filename) or fs.get_dir(filename):
        print(f"FILE {filename} already exists")
        # app.logger.info(f"file already exists {filename}")
        return Response("FAILED: file or dir with this name already exists", 409)
    # create file, return info about datanodes and id
    else:
        # app.logger.info(f"filesize: {filesize}   free_space:{fs.free_space}")
        if filesize > fs.free_space:  # check if there's available space
            print("FAILED: not enough space")
            return Response("not enough space", 413)
        else:
            file_dir = os.path.dirname(filename)
            file_name = os.path.basename(filename)
            file_parent = fs.get_dir(file_dir)
            if file_parent:
                if len(file_name) < 1:
                    return Response("FAILED: filename cannot be empty", 418)
                else:
                    file = fs.create_file(file_name, file_parent, filesize)
                    return jsonify({"file": file})
            else:
                return Response(f"FAILED: dir {file_dir} doesn't  exist", 404)


@app.route('/mkdir', methods=['POST'])
def mkdir():
    # get directory name
    dirname = request.json['dirname']

    if fs.get_file(dirname) or fs.get_dir(dirname):
        return Response("FAILED: dir or file with such name exists", 409)
    else:
        # add directory to fs tree
        dir_parent = os.path.dirname(dirname)
        dir_name = os.path.basename(dirname)
        parent_node = fs.get_dir(dir_parent)

        if parent_node:
            fs.create_directory(dir_name, parent_node)
            return Response("ok", 200)
        else:
            return Response(f"FAILED: specified path {dir_parent} does not exist", 404)


@app.route('/ls')
def ls():
    # get directory name
    dirname = request.json['dirname']
    dirs = []
    files = []

    # get directory from the dilesystem
    dir_node = fs.get_dir(dirname)

    if not dir_node:
        return Response(f"FAILED: specified directory {dirname} does not exist", 404)

    for node in dir_node.children:
        # check whether file or directory
        if node.is_file:
            files.append(node.name)
        else:
            dirs.append(node.name)
    return jsonify({'dirs': dirs, 'files': files})


@app.route('/cd', methods=['POST'])
def cd():
    # get directory name
    dirname = request.json['dirname']

    # get the directory from the filesystem
    node = fs.get_dir(dirname)
    if node:
        # change the current directory to the obtained one
        fs.cur_node = node
        return jsonify({'dirname': fs.cur_node.name, 'cur_dir': fs.get_current_dirname()})
    else:
        return Response(f'FAILED: specified directory {dirname} does not exist', 404)


@app.route('/info', methods=['POST'])
def info():
    # get file name
    filename = request.json['filename']
    # obtain the file from the filesystem
    node = fs.get_file(filename)
    if node:
        return jsonify({'info': node.file})
    else:
        return Response(f'FAILED: file {filename} not found', 404)


@app.route('/move', methods=['POST'])
def move():
    # get file name
    filename = request.json['filename']
    # get path
    path = request.json['path']

    # obtain file from the filesystem
    file_node = fs.get_file(filename)
    if file_node:
        # resolve filename
        dir_name = ''
        file_name = ''
        if path[-1] == '/':
            dir_name = path
            file_name = os.path.basename(filename)
        else:
            dir_name = os.path.dirname(path)
            file_name = os.path.basename(path)
        # obtain the directory where to move
        node = fs.get_dir(dir_name)
        if node:
            if file_name in [x.name for x in node.children]:
                return Response(f'FAILED: file {filename} already exists in this dir', 419)
            else:
                # move the file in a filesystem
                file_node.parent = node
                file_node.name = file_name
                return Response('', 200)
        else:
            return Response(f'FAILED: specified path {dir_name} does not exist', 404)
    else:
        return Response(f'FAILED: file {filename} could not be found', 404)


if __name__ == '__main__':
    heartbeat_thread = Thread(target=heartbeat)
    heartbeat_thread.start()
    app.run(debug=True, host=HOST, port=PORT)
    heartbeat_thread.join()
