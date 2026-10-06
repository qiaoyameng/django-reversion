Throws :ref:`RevisionManagementError` if it cannot determine which rows were written, for example when the
database cannot return automatically assigned primary keys from a bulk insert. In that case, perform the bulk
write yourself and use :ref:`add_to_revision` for the objects you know were saved.
