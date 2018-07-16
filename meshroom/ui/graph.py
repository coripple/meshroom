#!/usr/bin/env python
# coding:utf-8
import logging
import os
from threading import Thread

from PySide2.QtCore import Slot, QJsonValue, QObject, QUrl, Property, Signal

from meshroom.common.qt import QObjectListModel
from meshroom.core.attribute import Attribute, ListAttribute
from meshroom.core.graph import Graph, Edge, submitGraph, executeGraph
from meshroom.core.node import NodeChunk, Node, Status, CompatibilityNode
from meshroom.ui import commands


class ChunksMonitor(QObject):
    """
    ChunksMonitor regularly check NodeChunks' status files for modification and trigger their update on change.

    When working locally, status changes are reflected through the emission of 'statusChanged' signals.
    But when a graph is being computed externally - either via a Submitter or on another machine,
    NodeChunks status files are modified by another instance, potentially outside this machine file system scope.
    Same goes when status files are deleted/modified manually.
    Thus, for genericity, monitoring is based on regular polling and not file system watching.
    """
    def __init__(self, chunks=(), parent=None):
        super(ChunksMonitor, self).__init__(parent)
        self.lastModificationRecords = dict()
        self.setChunks(chunks)
        # Check status files every x seconds
        # TODO: adapt frequency according to graph compute status
        self.startTimer(5000)

    def setChunks(self, chunks):
        """ Set the list of chunks to monitor. """
        self.clear()
        for chunk in chunks:
            f = chunk.statusFile
            # Store a record of {chunk: status file last modification}
            self.lastModificationRecords[chunk] = self.getFileLastModTime(f)
            # For local use, handle statusChanged emitted directly from the node chunk
            chunk.statusChanged.connect(self.onChunkStatusChanged)
        self.chunkStatusChanged.emit(None, -1)

    def clear(self):
        """ Clear the list of monitored chunks """
        for ch in self.lastModificationRecords:
            ch.statusChanged.disconnect(self.onChunkStatusChanged)
        self.lastModificationRecords.clear()

    def timerEvent(self, evt):
        self.checkFileTimes()

    def onChunkStatusChanged(self):
        """ React to change of status coming from the NodeChunk itself. """
        chunk = self.sender()
        assert chunk in self.lastModificationRecords
        # Update record entry for this file so that it's up-to-date on next timerEvent
        self.lastModificationRecords[chunk] = self.getFileLastModTime(chunk.statusFile)
        self.chunkStatusChanged.emit(chunk, chunk.status.status)

    @staticmethod
    def getFileLastModTime(f):
        """ Return 'mtime' of the file if it exists, -1 otherwise. """
        return os.path.getmtime(f) if os.path.exists(f) else -1

    def checkFileTimes(self):
        """ Check status files last modification time and compare with stored value """
        for chunk, t in self.lastModificationRecords.items():
            lastMod = self.getFileLastModTime(chunk.statusFile)
            if lastMod != t:
                self.lastModificationRecords[chunk] = lastMod
                chunk.updateStatusFromCache()
                logging.debug("Status for node {} changed: {}".format(chunk.node, chunk.status.status))

    chunkStatusChanged = Signal(NodeChunk, int)


class UIGraph(QObject):
    """ High level wrapper over core.Graph, with additional features dedicated to UI integration.

    UIGraph exposes undoable methods on its graph and computation in a separate thread.
    It also provides a monitoring of all its computation units (NodeChunks).
    """
    def __init__(self, filepath='', parent=None):
        super(UIGraph, self).__init__(parent)
        self._undoStack = commands.UndoStack(self)
        self._graph = Graph('', self)
        self._modificationCount = 0
        self._chunksMonitor = ChunksMonitor(parent=self)
        self._chunksMonitor.chunkStatusChanged.connect(self.onChunkStatusChanged)
        self._computeThread = Thread()
        self._running = self._submitted = False
        self._sortedDFSChunks = QObjectListModel(parent=self)
        if filepath:
            self.load(filepath)

    def setGraph(self, g):
        """ Set the internal graph. """
        if self._graph:
            self.stopExecution()
            self.clear()
        self._graph = g
        self._graph.updated.connect(self.onGraphUpdated)
        self._graph.update()
        self.graphChanged.emit()

    def onGraphUpdated(self):
        """ Callback to any kind of attribute modification. """
        # TODO: handle this with a better granularity
        self.updateChunks()

    def updateChunks(self):
        dfsNodes = self._graph.dfsOnFinish(None)[0]
        chunks = self._graph.getChunks(dfsNodes)
        # Nothing has changed, return
        if self._sortedDFSChunks.objectList() == chunks:
            return
        self._sortedDFSChunks.setObjectList(chunks)
        # Update the list of monitored chunks
        self._chunksMonitor.setChunks(self._sortedDFSChunks)

    def clear(self):
        if self._graph:
            self._graph.deleteLater()
            self._graph = None
        self._sortedDFSChunks.clear()
        self._undoStack.clear()

    def load(self, filepath):
        g = Graph('')
        g.load(filepath)
        if not os.path.exists(g.cacheDir):
            os.mkdir(g.cacheDir)
        self.setGraph(g)

    @Slot(QUrl)
    def loadUrl(self, url):
        self.load(url.toLocalFile())

    @Slot(QUrl)
    def saveAs(self, url):
        self._graph.save(url.toLocalFile())
        self._undoStack.setClean()

    @Slot()
    def save(self):
        self._graph.save()
        self._undoStack.setClean()

    @Slot(Node)
    def execute(self, node=None):
        if self.computing:
            return
        nodes = [node] if node else None
        self._computeThread = Thread(target=self._execute, args=(nodes,))
        self._computeThread.start()

    def _execute(self, nodes):
        self.computeStatusChanged.emit()
        try:
            executeGraph(self._graph, nodes)
        except Exception as e:
            logging.error("Error during Graph execution {}".format(e))
        finally:
            self.computeStatusChanged.emit()

    @Slot()
    def stopExecution(self):
        if not self.isComputingLocally():
            return
        self._graph.stopExecution()
        self._computeThread.join()
        self.computeStatusChanged.emit()

    @Slot(Node)
    def submit(self, node=None):
        """ Submit the graph to the default Submitter.
        If a node is specified, submit this node and its uncomputed predecessors.
        Otherwise, submit the whole 

        Notes:
            Default submitter is specified using the MESHROOM_DEFAULT_SUBMITTER environment variable.
        """
        self.save()  # graph must be saved before being submitted
        node = [node] if node else None
        submitGraph(self._graph, os.environ.get('MESHROOM_DEFAULT_SUBMITTER', ''), node)

    def onChunkStatusChanged(self, chunk, status):
        # update graph computing status
        running = any([ch.status.status == Status.RUNNING for ch in self._sortedDFSChunks])
        submitted = any([ch.status.status == Status.SUBMITTED for ch in self._sortedDFSChunks])
        if self._running != running or self._submitted != submitted:
            self._running = running
            self._submitted = submitted
            self.computeStatusChanged.emit()

    def isComputing(self):
        """ Whether is graph is being computed, either locally or externally. """
        return self.isComputingLocally() or self.isComputingExternally()

    def isComputingExternally(self):
        """ Whether this graph is being computed externally. """
        return (self._running or self._submitted) and not self.isComputingLocally()

    def isComputingLocally(self):
        """ Whether this graph is being computed locally (i.e computation can be stopped). """
        return self._computeThread.is_alive()

    def push(self, command):
        """ Try and push the given command to the undo stack.

        Args:
            command (commands.UndoCommand): the command to push
        """
        return self._undoStack.tryAndPush(command)

    def groupedGraphModification(self, title, disableUpdates=True):
        """ Get a GroupedGraphModification for this Graph.

        Args:
            title (str): the title of the macro command
            disableUpdates (bool): whether to disable graph updates

        Returns:
            GroupedGraphModification: the instantiated context manager
        """
        return commands.GroupedGraphModification(self._graph, self._undoStack, title, disableUpdates)

    def beginModification(self, name):
        """ Begin a Graph modification. Calls to beginModification and endModification may be nested, but
        every call to beginModification must have a matching call to endModification. """
        self._modificationCount += 1
        self._undoStack.beginMacro(name)

    def endModification(self):
        """ Ends a Graph modification. Must match a call to beginModification. """
        assert self._modificationCount > 0
        self._modificationCount -= 1
        self._undoStack.endMacro()

    @Slot(str, result=QObject)
    def addNewNode(self, nodeType, **kwargs):
        """ [Undoable]
        Create a new Node of type 'nodeType' and returns it.

        Args:
            nodeType (str): the type of the Node to create.
            **kwargs: optional node attributes values
        Returns:
            Node: the created node
        """
        return self.push(commands.AddNodeCommand(self._graph, nodeType, **kwargs))

    @Slot(Node)
    def removeNode(self, node):
        self.push(commands.RemoveNodeCommand(self._graph, node))

    @Slot(Attribute, Attribute)
    def addEdge(self, src, dst):
        if isinstance(dst, ListAttribute) and not isinstance(src, ListAttribute):
            with self.groupedGraphModification("Insert and Add Edge on {}".format(dst.fullName())):
                self.appendAttribute(dst)
                self.push(commands.AddEdgeCommand(self._graph, src, dst.at(-1)))
        else:
            self.push(commands.AddEdgeCommand(self._graph, src, dst))

    @Slot(Edge)
    def removeEdge(self, edge):
        if isinstance(edge.dst.root, ListAttribute):
            with self.groupedGraphModification("Remove Edge and Delete {}".format(edge.dst.fullName())):
                self.push(commands.RemoveEdgeCommand(self._graph, edge))
                self.removeAttribute(edge.dst)
        else:
            self.push(commands.RemoveEdgeCommand(self._graph, edge))

    @Slot(Attribute, "QVariant")
    def setAttribute(self, attribute, value):
        self.push(commands.SetAttributeCommand(self._graph, attribute, value))

    @Slot(Attribute)
    def resetAttribute(self, attribute):
        """ Reset 'attribute' to its default value """
        self.push(commands.SetAttributeCommand(self._graph, attribute, attribute.defaultValue()))

    @Slot(Node, bool, result="QVariantList")
    def duplicateNode(self, srcNode, duplicateFollowingNodes=False):
        """
        Duplicate a node an optionally all the following nodes to graph leaves.

        Args:
            srcNode (Node): node to start the duplication from
            duplicateFollowingNodes (bool): whether to duplicate all the following nodes to graph leaves

        Returns:
            [Nodes]: the list of duplicated nodes
        """
        return self.push(commands.DuplicateNodeCommand(self._graph, srcNode, duplicateFollowingNodes))

    @Slot(CompatibilityNode)
    def upgradeNode(self, node):
        """ Upgrade a CompatibilityNode. """
        self.push(commands.UpgradeNodeCommand(self._graph, node))

    @Slot(Attribute, QJsonValue)
    def appendAttribute(self, attribute, value=QJsonValue()):
        if isinstance(value, QJsonValue):
            if value.isArray():
                pyValue = value.toArray().toVariantList()
            else:
                pyValue = None if value.isNull() else value.toObject()
        else:
            pyValue = value
        self.push(commands.ListAttributeAppendCommand(self._graph, attribute, pyValue))

    @Slot(Attribute)
    def removeAttribute(self, attribute):
        self.push(commands.ListAttributeRemoveCommand(self._graph, attribute))

    undoStack = Property(QObject, lambda self: self._undoStack, constant=True)
    graphChanged = Signal()
    graph = Property(Graph, lambda self: self._graph, notify=graphChanged)

    computeStatusChanged = Signal()
    computing = Property(bool, isComputing, notify=computeStatusChanged)
    computingExternally = Property(bool, isComputingExternally, notify=computeStatusChanged)
    computingLocally = Property(bool, isComputingLocally, notify=computeStatusChanged)

    sortedDFSChunks = Property(QObject, lambda self: self._sortedDFSChunks, constant=True)
    lockedChanged = Signal()
