package taskapi

import "net/http"

func (s *Server) fileVersion(w http.ResponseWriter, r *http.Request) {
	id, t, _, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	record, e := s.files.Version(r.Context(), t, id.UserID, r.PathValue("file_id"), r.PathValue("version_id"))
	if e != nil {
		fileError(w, e)
		return
	}
	fileJSON(w, 200, record)
}
